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
  an earlier sweep of the SAME job); landing is durable.
- :class:`PermanentLandingError` — conflict or invalid input; retrying cannot
  help. The reconciler reports it in the result (patch preserved) and closes
  the job out.
- :class:`TransientLandingError` — network/clone/push hiccup; the reconciler
  leaves the bucket object in place so the next sweep retries.

Credential handling: tokens are owner-allowlisted (same policy as the
Spark-side executor) and are passed to git ONLY via a ``GIT_ASKPASS`` helper
reading process-private environment variables — never on the command line
(argv is world-readable in /proc) and never written into ``.git/config``.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

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


# Mirrors AgenticRepoExecutor's owner policy on the Spark side (the two halves
# of the bridge enforce the same allowlist with their own tokens). Returns the
# (username, token) pair for the host, or (None, None) if no credential should
# be attached for this repo.
def _credentials_for(repo_url: str) -> tuple[str | None, str | None]:
    parsed = urlparse(repo_url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return None, None
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
        return None, None
    segments = [seg for seg in parsed.path.split("/") if seg]
    if not segments or ".." in segments:
        log.warning("refusing token: suspicious repo path %r", parsed.path)
        return None, None
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
        return None, None
    return username, token


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


_ASKPASS_SCRIPT = """#!/bin/sh
# spark_relay lander GIT_ASKPASS helper: answers git's username/password
# prompts from process-private env vars so tokens never appear in argv.
case "$1" in
  Username*) printf '%s' "$LANDER_GIT_USERNAME" ;;
  Password*) printf '%s' "$LANDER_GIT_PASSWORD" ;;
esac
"""


def _branch_for_job(job_id: str) -> str:
    """Collision-resistant review branch name: the FULL sanitized job id.

    Job ids are UUIDv7 whose first 12 chars are a millisecond timestamp —
    burst-submitted jobs share that prefix (the 2026-06-05 session-collision
    incident), so a truncated prefix would alias different jobs' branches.
    The sanitizer is not injective ("a/b" and "a b" both collapse to "a-b"),
    so any id the sanitizer had to alter gets a hash suffix of the RAW id.
    """
    raw = str(job_id)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-")
    if not safe:
        raise PermanentLandingError("unusable job id")
    if safe != raw:
        safe = f"{safe}-{hashlib.sha256(raw.encode()).hexdigest()[:8]}"
    return f"hive/spark-{safe}"


class PatchLander:
    """Apply a Spark format-patch onto the canonical repo and push a review branch.

    Clones are cached per-repo under ``cache_dir`` (default
    ``~/.cache/spark-relay-lander``) so repeated landings only pay a fetch.
    All operations on a cached clone hold a per-repo ``flock`` so concurrent
    reconcilers (or a sweep overlapping a slow push) cannot corrupt the
    shared worktree.
    """

    def __init__(self, cache_dir: str | None = None):
        self.cache_dir = Path(
            cache_dir
            or os.environ.get("SPARK_LANDER_CACHE")
            or Path.home() / ".cache" / "spark-relay-lander"
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.chmod(0o700)  # clones + orphan patches are private code
        self.orphan_dir = self.cache_dir / "orphans"
        self.orphan_dir.mkdir(exist_ok=True)
        self.orphan_dir.chmod(0o700)
        self.git_timeout = float(os.environ.get("SPARK_LANDER_GIT_TIMEOUT", "300"))
        self._askpass = self.cache_dir / "askpass.sh"
        self._askpass.write_text(_ASKPASS_SCRIPT)
        self._askpass.chmod(stat.S_IRWXU)

    # -- public ----------------------------------------------------------

    def land(self, payload: dict, job_id: str) -> dict:
        """Land ``payload['patch']`` as ``hive/spark-<jobid>`` on ``payload['repo']``.

        Returns ``{"landed_branch", "landed_repo", "landed_sha"}`` on success.
        Raises :class:`PermanentLandingError` / :class:`TransientLandingError`.
        """
        patch = payload.get("patch")
        if not isinstance(patch, str) or not patch.strip():
            # Non-string truthy values would AttributeError past the typed
            # error paths and wedge the bucket object in an eternal retry.
            raise PermanentLandingError("payload has no usable patch text")
        repo_url = str(payload.get("repo") or "").strip()
        if not repo_url:
            raise PermanentLandingError("payload has no repo url")
        parsed = urlparse(repo_url)
        if parsed.scheme != "https":
            raise PermanentLandingError(f"non-https repo url: {_redact_secrets(repo_url)!r}")
        if parsed.username or parsed.password:
            # Embedded credentials in a relayed payload are never acceptable —
            # and must never be echoed into results/logs.
            raise PermanentLandingError(
                f"repo url carries embedded credentials, refusing: {_redact_secrets(repo_url)}"
            )
        username, token = _credentials_for(repo_url)
        if not token:
            # A push would fail with auth prompts; permanent so the patch is
            # preserved (result + orphan file) for manual landing.
            raise PermanentLandingError(
                f"no push credentials for {_redact_secrets(repo_url)} "
                "(owner not allowlisted or token unset)"
            )
        env = self._git_env(username, token)
        branch = _branch_for_job(job_id)

        with self._repo_lock(repo_url):
            # Idempotency: an earlier sweep may have pushed the branch and then
            # died before the hive PATCH. The branch name embeds the FULL job
            # id, so an existing branch can only be THIS job's earlier landing.
            existing = self._ls_remote_branch(repo_url, branch, env)
            if existing:
                log.info("branch %s already on %s at %s — reusing", branch, repo_url, existing[:12])
                return {"landed_branch": branch, "landed_repo": repo_url, "landed_sha": existing}

            clone = self._ensure_clone(repo_url, env)
            # Identity is required by `git am` ("Committer identity unknown").
            # Set it unconditionally: a crash between a fresh clone's rename
            # and its config calls would otherwise leave a clone that fails
            # permanently on every retry.
            self._git(clone, ["config", "user.name", os.environ.get("SPARK_GIT_USER_NAME", "Spark Lander")])
            self._git(
                clone,
                ["config", "user.email", os.environ.get("SPARK_GIT_USER_EMAIL", "jperlow@gmail.com")],
            )
            default = self._default_branch(clone)
            # Restore a pristine HEAD no matter what a previous (crashed/timed
            # out) landing left behind: stale am state, dirty tracked files,
            # stray untracked files.
            self._git(clone, ["am", "--abort"], check=False)
            self._git(clone, ["checkout", "-q", "--detach", f"origin/{default}"])
            self._git(clone, ["reset", "--hard", f"origin/{default}"], check=False)
            self._git(clone, ["clean", "-fdq"], check=False)

            with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as fh:
                fh.write(patch if patch.endswith("\n") else patch + "\n")
                patch_file = fh.name
            try:
                proc = self._git(clone, ["am", "-3", patch_file], check=False)
                if proc.returncode != 0:
                    self._git(clone, ["am", "--abort"], check=False)
                    err = _redact_secrets((proc.stderr or proc.stdout).strip()[-400:])
                    raise PermanentLandingError(
                        f"patch does not apply onto origin/{default}: {err}"
                    )
            finally:
                os.unlink(patch_file)

            sha = self._git(clone, ["rev-parse", "HEAD"]).stdout.strip()
            push = self._git(
                clone, ["push", "origin", f"HEAD:refs/heads/{branch}"], env=env, check=False
            )
            if push.returncode != 0:
                # Another reconciler may have raced us past the ls-remote check
                # (the flock is per-host only). If the branch now exists, the
                # landing IS durable — report the winner's sha.
                raced = self._ls_remote_branch(repo_url, branch, env)
                if raced:
                    log.info("lost push race for %s — branch exists at %s", branch, raced[:12])
                    return {"landed_branch": branch, "landed_repo": repo_url, "landed_sha": raced}
                err = _redact_secrets((push.stderr or push.stdout).strip()[-400:])
                raise _classify_git_failure(err)(f"push of {branch} failed: {err}")
        log.info("landed %s -> %s %s (%s)", job_id, repo_url, branch, sha[:12])
        return {"landed_branch": branch, "landed_repo": repo_url, "landed_sha": sha}

    def save_orphan_patch(self, payload: dict, job_id: str) -> str | None:
        """Durably save a patch that could NOT be landed (and may be too large
        to survive in the hive result). Returns the saved path, or None."""
        patch = payload.get("patch")
        if not patch:
            return None
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(job_id)).strip("-") or "unknown"
        path = self.orphan_dir / f"{safe}.patch"
        body = str(patch)
        if not body.endswith("\n"):
            body += "\n"
        try:
            # 0600 from creation (no chmod-after-write exposure window).
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(body)
            return str(path)
        except OSError as exc:
            log.error("could not save orphan patch for %s: %s", job_id, exc)
            return None

    # -- internals ---------------------------------------------------------

    def _git_env(self, username: str, token: str) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            GIT_ASKPASS=str(self._askpass),
            GIT_TERMINAL_PROMPT="0",
            LANDER_GIT_USERNAME=username,
            LANDER_GIT_PASSWORD=token,
            # Disable credential helpers for every command holding lander
            # credentials: a configured helper could (a) answer auth with a
            # DIFFERENT cached credential, bypassing the owner/token policy,
            # and (b) on success PERSIST the lander token into
            # ~/.git-credentials / keychain / cache daemon. An empty
            # credential.helper value clears the helper list.
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="credential.helper",
            GIT_CONFIG_VALUE_0="",
        )
        return env

    def _repo_lock(self, repo_url: str):
        lock_path = self._repo_cache_path(repo_url).with_suffix(".lock")

        class _Lock:
            def __enter__(_self):
                _self.fh = open(lock_path, "w")
                fcntl.flock(_self.fh, fcntl.LOCK_EX)
                return _self

            def __exit__(_self, *exc):
                fcntl.flock(_self.fh, fcntl.LOCK_UN)
                _self.fh.close()

        return _Lock()

    def _repo_cache_path(self, repo_url: str) -> Path:
        digest = hashlib.sha256(repo_url.encode()).hexdigest()[:16]
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", urlparse(repo_url).path.strip("/"))[:48]
        return self.cache_dir / f"{slug}-{digest}"

    def _ensure_clone(self, repo_url: str, env: dict[str, str]) -> Path:
        clone = self._repo_cache_path(repo_url)
        if (clone / ".git").is_dir():
            proc = self._git(
                clone,
                ["fetch", "origin", "+refs/heads/*:refs/remotes/origin/*", "--prune"],
                env=env,
                check=False,
            )
            if proc.returncode != 0:
                err = _redact_secrets((proc.stderr or proc.stdout).strip()[-400:])
                raise _classify_git_failure(err)(f"fetch failed: {err}")
            return clone
        if clone.exists():
            # Leftover of a clone that crashed/timed out before completing —
            # without a .git dir it can only be garbage; clear it so the fresh
            # clone below does not fail on "destination path already exists".
            shutil.rmtree(clone, ignore_errors=True)
        # Clone with the CLEAN url into a temp dir and rename into place
        # atomically, so a half-written clone can never be mistaken for a
        # cache hit. The askpass env supplies credentials, so nothing secret
        # ever reaches argv or .git/config.
        tmp = clone.with_name(clone.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            proc = subprocess.run(
                ["git", "clone", "--quiet", repo_url, str(tmp)],
                capture_output=True,
                text=True,
                timeout=self.git_timeout * 4,  # first clone of a big repo is slow
                env=env,
            )
        except subprocess.TimeoutExpired:
            shutil.rmtree(tmp, ignore_errors=True)
            raise TransientLandingError(f"clone timed out after {self.git_timeout * 4:.0f}s")
        if proc.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            err = _redact_secrets((proc.stderr or proc.stdout).strip()[-400:])
            raise _classify_git_failure(err)(f"clone failed: {err}")
        tmp.rename(clone)
        return clone

    def _default_branch(self, clone: Path) -> str:
        proc = self._git(clone, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip().split("/", 1)[-1]
        for cand in ("main", "master"):
            if self._git(clone, ["rev-parse", "--verify", f"origin/{cand}"], check=False).returncode == 0:
                return cand
        raise PermanentLandingError("cannot determine default branch")

    def _ls_remote_branch(self, repo_url: str, branch: str, env: dict[str, str]) -> str | None:
        try:
            proc = subprocess.run(
                ["git", "ls-remote", repo_url, f"refs/heads/{branch}"],
                capture_output=True,
                text=True,
                timeout=self.git_timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            raise TransientLandingError(f"ls-remote timed out after {self.git_timeout:.0f}s")
        if proc.returncode != 0:
            err = _redact_secrets((proc.stderr or proc.stdout).strip()[-400:])
            raise _classify_git_failure(err)(f"ls-remote failed: {err}")
        line = proc.stdout.strip()
        return line.split("\t", 1)[0] if line else None

    def _git(
        self,
        cwd: Path,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self.git_timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            # A hung network operation must NOT close the job out permanently;
            # the next sweep starts from a pristine reset of the cached clone.
            raise TransientLandingError(
                f"git {args[0]} timed out after {self.git_timeout:.0f}s"
            )
        if check and proc.returncode != 0:
            raise PermanentLandingError(
                _redact_secrets(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()[-400:]}")
            )
        return proc
