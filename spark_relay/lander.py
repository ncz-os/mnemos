"""PYTHIA-side patch lander: needs-review patch -> hive/spark-<jobid> branch.

The Spark executor deliberately never pushes (the Spark host is isolated from
fleet git credentials); it ships a ``git format-patch`` through the relay
bucket instead. Until 2026-06-07 the reconciler done-marked those payloads
with the patch trapped inside the job result — 73 jobs orphaned that way.
The lander closes the loop: apply the patch onto a fresh checkout of the
canonical repository and push it as a ``hive/spark-<jobid>`` review branch,
so Spark work always lands as reviewable git state instead of JSON cargo.

Outcomes:

- dict from :meth:`PatchLander.land` — branch pushed (or already present from
  an earlier sweep); landing is durable.
- :class:`PermanentLandingError` — conflict or invalid input; retrying cannot
  help. The reconciler reports it in the result (patch preserved) and closes
  the job out.
- :class:`TransientLandingError` — network/clone/push hiccup; the reconciler
  leaves the bucket object in place so the next sweep retries.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse, urlunparse

log = logging.getLogger("spark_relay.lander")


class LandingError(Exception):
    """Base for landing failures."""


class PermanentLandingError(LandingError):
    """Landing cannot succeed by retrying (conflict, bad input, rejected push)."""


class TransientLandingError(LandingError):
    """Landing may succeed on a later sweep (network, lock, 5xx)."""


def _redact_secrets(text: object) -> str:
    """Strip inline git creds (https://user:token@host) from error text/logs."""
    try:
        return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", str(text))
    except Exception:  # noqa: BLE001
        return "<redacted>"


# Mirrors AgenticRepoExecutor._credentialed_url on the Spark side (the two
# halves of the bridge enforce the same owner policy with their own tokens).
def _credentialed_url(repo_url: str) -> str:
    parsed = urlparse(repo_url)
    if parsed.scheme != "https":
        return repo_url
    if parsed.username or parsed.password:
        return repo_url
    host = (parsed.hostname or "").rstrip(".").lower()
    token = None
    username = None
    if host == "github.com":
        token = os.environ.get("GITHUB_TOKEN")
        username = "x-access-token"
    elif host == "gitlab.com":
        token = os.environ.get("GITLAB_TOKEN")
        username = "oauth2"
    elif host == "codeberg.org":
        token = os.environ.get("CODEBERG_TOKEN")
        username = os.environ.get("CODEBERG_USER", "jperlow")
    if not token or not username:
        return repo_url
    segments = [seg for seg in parsed.path.split("/") if seg]
    if not segments or ".." in segments:
        log.warning("refusing token: suspicious repo path %r", parsed.path)
        return repo_url
    owner = segments[0]
    allowed_owners = {
        o.strip()
        for o in os.environ.get(
            "SPARK_TOKEN_OWNERS",
            "perlowja,jperlow,nclawzero,ncz-os,mnemos-os,argonautsystems",
        ).split(",")
        if o.strip()
    }
    if owner not in allowed_owners:
        log.warning("refusing to attach token for non-fleet owner: %s", owner)
        return repo_url
    netloc = f"{username}:{token}@{host}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


_TRANSIENT_GIT_MARKERS = (
    "could not resolve host",
    "couldn't connect",
    "connection timed out",
    "connection refused",
    "connection reset",
    "operation timed out",
    "early eof",
    "rpc failed",
    "503",
    "502",
    "500",
    "the remote end hung up",
    "unable to access",
    "index.lock",
)


def _classify_git_failure(stderr: str) -> type[LandingError]:
    low = stderr.lower()
    if any(m in low for m in _TRANSIENT_GIT_MARKERS):
        return TransientLandingError
    return PermanentLandingError


def _job_id_short(job_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(job_id))[:12] or "manual"


class PatchLander:
    """Apply a Spark format-patch onto the canonical repo and push a review branch.

    Clones are cached per-repo under ``cache_dir`` (default
    ``~/.cache/spark-relay-lander``) so repeated landings only pay a fetch.
    The remote URL stored in the cache clone is the CLEAN url; credentials are
    passed per-command and never written to .git/config.
    """

    def __init__(self, cache_dir: str | None = None):
        self.cache_dir = Path(
            cache_dir
            or os.environ.get("SPARK_LANDER_CACHE")
            or Path.home() / ".cache" / "spark-relay-lander"
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.git_timeout = float(os.environ.get("SPARK_LANDER_GIT_TIMEOUT", "300"))

    # -- public ----------------------------------------------------------

    def land(self, payload: dict, job_id: str) -> dict:
        """Land ``payload['patch']`` as ``hive/spark-<jobid>`` on ``payload['repo']``.

        Returns ``{"landed_branch", "landed_repo", "landed_sha"}`` on success.
        Raises :class:`PermanentLandingError` / :class:`TransientLandingError`.
        """
        patch = payload.get("patch")
        if not patch or not str(patch).strip():
            raise PermanentLandingError("payload has no patch text")
        repo_url = str(payload.get("repo") or "").strip()
        if not repo_url:
            raise PermanentLandingError("payload has no repo url")
        if urlparse(repo_url).scheme != "https":
            raise PermanentLandingError(f"non-https repo url: {repo_url!r}")
        cred_url = _credentialed_url(repo_url)
        if cred_url == repo_url:
            # No token attached: either the owner is outside the fleet
            # allowlist or no token is configured for the host. A push would
            # fail with auth prompts; treat as permanent so the patch is
            # preserved in the result for manual landing.
            raise PermanentLandingError(
                f"no push credentials for {repo_url} (owner not allowlisted or token unset)"
            )

        branch = f"hive/spark-{_job_id_short(job_id)}"

        # Idempotency: an earlier sweep may have pushed the branch and then
        # died before the hive PATCH; re-landing must not fail on "exists".
        existing = self._ls_remote_branch(cred_url, branch)
        if existing:
            log.info("branch %s already on %s at %s — reusing", branch, repo_url, existing[:12])
            return {"landed_branch": branch, "landed_repo": repo_url, "landed_sha": existing}

        clone = self._ensure_clone(repo_url, cred_url)
        default = self._default_branch(clone)
        self._git(clone, ["am", "--abort"], check=False)  # clear any stale state
        self._git(clone, ["checkout", "-q", "--detach", f"origin/{default}"])
        self._git(clone, ["clean", "-fdq"], check=False)

        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as fh:
            fh.write(patch if patch.endswith("\n") else patch + "\n")
            patch_file = fh.name
        try:
            proc = self._git(clone, ["am", "-3", patch_file], check=False)
            if proc.returncode != 0:
                self._git(clone, ["am", "--abort"], check=False)
                err = _redact_secrets((proc.stderr or proc.stdout).strip()[-400:])
                raise PermanentLandingError(f"patch does not apply onto origin/{default}: {err}")
        finally:
            os.unlink(patch_file)

        sha = self._git(clone, ["rev-parse", "HEAD"]).stdout.strip()
        push = self._git(clone, ["push", cred_url, f"HEAD:refs/heads/{branch}"], check=False)
        if push.returncode != 0:
            err = _redact_secrets((push.stderr or push.stdout).strip()[-400:])
            raise _classify_git_failure(err)(f"push of {branch} failed: {err}")
        log.info("landed %s -> %s %s (%s)", job_id, repo_url, branch, sha[:12])
        return {"landed_branch": branch, "landed_repo": repo_url, "landed_sha": sha}

    # -- internals ---------------------------------------------------------

    def _repo_cache_path(self, repo_url: str) -> Path:
        digest = hashlib.sha256(repo_url.encode()).hexdigest()[:16]
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", urlparse(repo_url).path.strip("/"))[:48]
        return self.cache_dir / f"{slug}-{digest}"

    def _ensure_clone(self, repo_url: str, cred_url: str) -> Path:
        clone = self._repo_cache_path(repo_url)
        if (clone / ".git").is_dir():
            proc = self._git(clone, ["fetch", cred_url, "+refs/heads/*:refs/remotes/origin/*", "--prune"], check=False)
            if proc.returncode != 0:
                err = _redact_secrets((proc.stderr or proc.stdout).strip()[-400:])
                raise _classify_git_failure(err)(f"fetch failed: {err}")
            return clone
        proc = subprocess.run(
            ["git", "clone", "--quiet", cred_url, str(clone)],
            capture_output=True,
            text=True,
            timeout=self.git_timeout * 4,  # first clone of a big repo is slow
        )
        if proc.returncode != 0:
            err = _redact_secrets((proc.stderr or proc.stdout).strip()[-400:])
            raise _classify_git_failure(err)(f"clone failed: {err}")
        # Never leave credentials on disk: replace origin with the clean URL.
        self._git(clone, ["remote", "set-url", "origin", repo_url])
        self._git(clone, ["config", "user.name", os.environ.get("SPARK_GIT_USER_NAME", "Spark Lander")])
        self._git(
            clone,
            ["config", "user.email", os.environ.get("SPARK_GIT_USER_EMAIL", "jperlow@gmail.com")],
        )
        return clone

    def _default_branch(self, clone: Path) -> str:
        proc = self._git(clone, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip().split("/", 1)[-1]
        for cand in ("main", "master"):
            if self._git(clone, ["rev-parse", "--verify", f"origin/{cand}"], check=False).returncode == 0:
                return cand
        raise PermanentLandingError("cannot determine default branch")

    def _ls_remote_branch(self, cred_url: str, branch: str) -> str | None:
        proc = subprocess.run(
            ["git", "ls-remote", cred_url, f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            timeout=self.git_timeout,
        )
        if proc.returncode != 0:
            err = _redact_secrets((proc.stderr or proc.stdout).strip()[-400:])
            raise _classify_git_failure(err)(f"ls-remote failed: {err}")
        line = proc.stdout.strip()
        return line.split("\t", 1)[0] if line else None

    def _git(self, cwd: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=self.git_timeout,
        )
        if check and proc.returncode != 0:
            raise PermanentLandingError(
                _redact_secrets(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()[-400:]}")
            )
        return proc
