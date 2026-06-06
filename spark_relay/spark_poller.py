"""Spark-side poller: pending/ -> claim -> execute -> results/.

Runs ON the DGX Spark (``spark-0c53``), which is network-isolated from the home
fleet. It imports ONLY :mod:`relay_crypto` and :mod:`relay_client` — never
``bridge_common`` (no hive/MNEMOS reachability). Loop:

    list pending  ->  conditional-claim each  ->  (if won) open  ->  execute on
    host-locked NGC model  ->  seal result  ->  put results/ (or failed/)

The actual agentic work (run the model, edit the repo, commit, push to GitHub)
is delegated to an :class:`Executor`. A reference NGC-chat executor is provided;
swap in the full agentic runtime by passing ``--executor`` or editing
:func:`make_executor`. The relay plumbing is complete and round-trippable with
the stub.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse, urlunparse

from . import relay_crypto
from .relay_client import RelayClient

log = logging.getLogger("spark_relay.poller")


def _redact_secrets(text):
    """Strip inline git creds (https://user:token@host) from error text/logs."""
    try:
        return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", str(text))
    except Exception:
        return "<redacted>"


# Fairness cap (operator 2026-06-04): per sweep, claim at most N jobs so Spark
# stays heavily on NGC but does NOT drain the queue — overflow spills to the
# home fleet. Tune via SPARK_MAX_PER_SWEEP.
MAX_PER_SWEEP = int(os.environ.get("SPARK_MAX_PER_SWEEP", "4"))

# Bucket status object name for this host's GPU telemetry (PYTHIA enqueuer reads
# it and folds it into the spark-0c53 agent metadata so it shows in the hive
# dashboard's GPU panel). Host-scoped so multiple remote workers don't collide.
GPU_STATUS_NAME = (os.environ.get("SPARK_GPU_HOST") or "spark-0c53") + "-gpu"

NONCOMMIT_PREFIXES = (
    "architecture",
    "analysis",
    "research",
    "triage",
    "review",
    "design",
    "docs:",
    "investigation",
    "track:",
    "ops:",
    "diag:",
    "ping:",
    "hive-stats",
    "dream-walker",
)

KIND_WORKSPACE_MAP = (
    (("ic-engine:", "investorclaw:"), "https://gitlab.com/argonautsystems/ic-engine.git"),
    (("riskyeats:",), "https://gitlab.com/perlowja/riskyeats.git"),
    (("riskybiz:", "argonaut:"), None),
    (
        ("mnemos:", "feat:knemon", "fix:knemon", "feat:oracle-backend"),
        "https://gitlab.com/mnemos-os/mnemos.git",
    ),
    (("ncz-os-zeroclaw:",), "https://gitlab.com/nclawzero/zeroclaw.git"),
    (("ncz-os-",), "https://gitlab.com/nclawzero/ncz-installer.git"),
    (("fleet-infra:",), None),
)

REPO_HINT_RE = re.compile(r"(?im)^\s*repo:\s*(?P<repo>\S+)\s*$")


def worker_id() -> str:
    return os.environ.get("SPARK_WORKER_ID") or socket.gethostname()


def _nvidia_smi(query: str) -> list[list[str]]:
    try:
        r = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=6,
        )
        return [[c.strip() for c in ln.split(",")] for ln in r.stdout.strip().splitlines() if ln.strip()]
    except Exception:  # noqa: BLE001 — telemetry is best-effort
        return []


def gpu_snapshot() -> dict:
    """Collect this host's GPU specs + runtime in the shape /v1/hosts expects."""

    def _num(v, cast):
        try:
            return cast(v)
        except (ValueError, TypeError):
            return None

    specs, runtime = [], []
    for p in _nvidia_smi("name,memory.total,driver_version"):
        if len(p) >= 2:
            specs.append(
                {
                    "vendor": "nvidia",
                    "name": p[0],
                    "vram_mib": _num(p[1], int),
                    "driver": p[2] if len(p) > 2 else None,
                }
            )
    for p in _nvidia_smi("name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"):
        if len(p) >= 5:
            runtime.append(
                {
                    "vendor": "nvidia",
                    "name": p[0],
                    "util_pct": _num(p[1], float),
                    "mem_used_mib": _num(p[2], int),
                    "mem_total_mib": _num(p[3], int),
                    "temp_c": _num(p[4], float),
                    "power_w": _num(p[5], float) if len(p) > 5 else None,
                }
            )
    return {"specs_gpus": specs, "gpus_runtime": runtime}


def report_gpu(relay: RelayClient, key: bytes) -> None:
    """Write this host's GPU snapshot to the bucket (best-effort)."""
    try:
        snap = gpu_snapshot()
        relay.put_status(
            GPU_STATUS_NAME, relay_crypto.seal(snap, key, aad=relay_crypto.aad_for("status", GPU_STATUS_NAME))
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("gpu report failed: %s", exc)


class Executor(Protocol):
    def execute(self, job: dict) -> dict:
        """Run the job. Return ``{commit_sha, branch, metrics}`` or raise."""
        ...


class OpenAIChatExecutor:
    """Calls any OpenAI-compatible /chat/completions endpoint (local llama.cpp /
    ollama / vLLM, or NGC). The local GB10 coder is the primary; NGC is the
    cloud fallback. Does NOT yet edit/commit/push a repo — that is the
    integration point for the full agentic runtime (see module docstring).
    """

    def __init__(self, base: str, api_key: str, default_model: str, *, label: str, timeout: float):
        self.base = base.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model
        self.label = label
        self.timeout = timeout

    def complete(self, *, system: str, user: str, model: str | None = None, timeout: float | None = None) -> str:
        import requests

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        resp = requests.post(
            f"{self.base}/chat/completions",
            headers=headers,
            json={
                "model": model or self.default_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=timeout or self.timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def execute(self, job: dict) -> dict:
        model = job.get("model") or self.default_model
        context = "\n\n".join(c["content"] for c in job.get("context", []))
        sys_prompt = (
            f"You are a Spark coding worker. Use the provided MNEMOS context.\n\nCONTEXT:\n{context}"
            if context
            else "You are a Spark coding worker."
        )
        out = self.complete(system=sys_prompt, user=job["prompt"], model=model)
        # TODO(agentic): apply edits, git commit, git push -> set commit_sha/branch.
        return {
            "commit_sha": None,
            "branch": job.get("branch"),
            "metrics": {"backend": self.label, "model": model, "output_chars": len(out), "output": out},
        }


class FallbackExecutor:
    """Try executors in order; use the first that succeeds. Lets the local GB10
    coder serve by default and fall back to NGC only when local is down."""

    def __init__(self, chain: list[tuple[str, OpenAIChatExecutor]]):
        self.chain = chain

    def execute(self, job: dict) -> dict:
        errors = []
        for name, ex in self.chain:
            try:
                return ex.execute(job)
            except Exception as exc:  # noqa: BLE001 — try the next backend
                log.warning("executor %s failed, trying next: %s", name, exc)
                errors.append(f"{name}: {exc}")
        raise RuntimeError("all executors failed: " + " | ".join(errors))


class AgenticRepoExecutor:
    """Clone a mapped repo, ask NGC for a git-applyable diff, and make a local
    review commit. This executor deliberately returns patches only; it never
    pushes branches to upstream repositories.
    """

    def __init__(self, chat: OpenAIChatExecutor):
        self.chat = chat
        self.git_timeout = float(os.environ.get("SPARK_GIT_TIMEOUT", "120"))
        self.model_timeout = float(os.environ.get("SPARK_AGENTIC_MODEL_TIMEOUT", str(chat.timeout)))

    def execute(self, job: dict) -> dict:
        repo_url = self._resolve_repo_url(job)
        if not repo_url:
            return {
                "status": "needs-review",
                "error": "no repo mapping for kind",
                "suggestion": self._chat_suggestion(job),
            }

        workdir = tempfile.mkdtemp(prefix="spark-repo-")
        clone_dir = Path(workdir) / "repo"
        try:
            clone_url = self._credentialed_url(repo_url)
            self._git(["clone", "--depth", "1", clone_url, str(clone_dir)], cwd=Path(workdir))
            tree = self._repo_tree(clone_dir)
            named_files = self._named_file_context(clone_dir, self._job_text(job))
            model_text = self._request_diff(job, tree, named_files)
            diff = self._extract_diff(model_text)
            if not diff:
                return {
                    "status": "needs-review",
                    "error": "model diff did not apply cleanly",
                    "suggestion": model_text,
                    "repo": repo_url,
                }

            diff_path = Path(workdir) / "spark.diff"
            diff_path.write_text(diff, encoding="utf-8")
            check = self._git(["apply", "--check", str(diff_path)], cwd=clone_dir, check=False)
            if check.returncode != 0:
                return {
                    "status": "needs-review",
                    "error": "model diff did not apply cleanly",
                    "suggestion": model_text,
                    "repo": repo_url,
                }

            self._git(["apply", str(diff_path)], cwd=clone_dir)
            self._git(["config", "user.name", os.environ.get("SPARK_GIT_USER_NAME", "Spark Nemotron")], cwd=clone_dir)
            self._git(
                ["config", "user.email", os.environ.get("SPARK_GIT_USER_EMAIL", "spark-nemotron@localhost")],
                cwd=clone_dir,
            )
            branch = f"spark/{self._job_id_short(job)}"
            self._git(["checkout", "-b", branch], cwd=clone_dir)
            self._git(["add", "-A"], cwd=clone_dir)
            changed = self._git(["diff", "--cached", "--name-only"], cwd=clone_dir).stdout.splitlines()
            if not changed:
                return {
                    "status": "needs-review",
                    "error": "model diff did not apply cleanly",
                    "suggestion": model_text,
                    "repo": repo_url,
                }
            self._git(["commit", "-m", self._commit_message(job)], cwd=clone_dir)
            sha = self._git(["rev-parse", "HEAD"], cwd=clone_dir).stdout.strip()
            patch = self._git(["format-patch", "-1", "--stdout"], cwd=clone_dir).stdout
            return {
                "status": "needs-review",
                "patch": patch,
                "commit_sha": sha,
                "branch": branch,
                "repo": repo_url,
                "files_changed": changed,
                "metrics": {"backend": "ngc-agentic", "model": self.chat.default_model},
            }
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _chat_suggestion(self, job: dict) -> str:
        try:
            return self.chat.execute(job)["metrics"]["output"]
        except Exception as exc:  # noqa: BLE001
            return f"chat suggestion failed: {exc}"

    def _request_diff(self, job: dict, tree: str, named_files: str) -> str:
        task = self._job_text(job)
        system = (
            "You are a repository editing agent. Output ONLY a single unified git diff "
            "that is compatible with git apply -p1. Do not include prose or markdown fences."
        )
        user = (
            f"Repo tree:\n{tree}\n\n"
            f"Relevant file excerpts:\n{named_files or '(none)'}\n\n"
            f"Task:\n{task}\n\n"
            "Output ONLY the unified git diff."
        )
        return self.chat.complete(system=system, user=user, timeout=self.model_timeout)

    def _resolve_repo_url(self, job: dict) -> str | None:
        hint = REPO_HINT_RE.search(self._job_text(job))
        if hint:
            return self._repo_hint_to_url(hint.group("repo"))
        kind = str(job.get("kind") or "")
        for prefixes, url in KIND_WORKSPACE_MAP:
            if kind.startswith(prefixes):
                return url
        return None

    def _repo_hint_to_url(self, repo: str) -> str | None:
        # Spark scope: open source + NVIDIA-internal only. Commercial
        # argonautsystems projects (ic-engine, riskyeats, florida-licenses) are
        # intentionally excluded and must not be cloned/committed from Spark.
        aliases = {
            "mnemos": "https://gitlab.com/mnemos-os/mnemos.git",
            "zeroclaw": "https://gitlab.com/nclawzero/zeroclaw.git",
            "ncz-installer": "https://gitlab.com/nclawzero/ncz-installer.git",
            "fleet-ops": None,
        }
        # Optional operator-managed extra allowlist:
        # SPARK_REPO_ALLOWLIST="name=https://host/owner/repo.git,other=https://..."
        for entry in os.environ.get("SPARK_REPO_ALLOWLIST", "").split(","):
            entry = entry.strip()
            if "=" in entry:
                name, url = entry.split("=", 1)
                aliases.setdefault(name.strip(), url.strip() or None)
        # SECURITY: job-supplied repo targets are allowlist-only. Raw URLs and
        # bare "owner/repo" hints are rejected so a queued job cannot force a
        # clone of an arbitrary Git server (SSRF) or attach credentialed clone
        # URLs to an attacker-chosen repository.
        if repo in aliases:
            return aliases[repo]
        logging.warning("rejected non-allowlisted repo hint: %r", repo)
        return None

    def _credentialed_url(self, repo_url: str) -> str:
        parsed = urlparse(repo_url)
        if parsed.scheme != "https":
            return repo_url
        # Never re-credential a URL that already carries userinfo.
        if parsed.username or parsed.password:
            return repo_url
        # Normalise host (lowercase, drop trailing dot) before matching.
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
        # SECURITY: only attach a Git token when the repository is owned by a
        # known fleet org. Reject empty path segments and traversal so a
        # crafted path cannot route credentials to an unexpected owner.
        segments = [seg for seg in parsed.path.split("/") if seg]
        if not segments or ".." in segments:
            logging.warning("refusing token: suspicious repo path %r", parsed.path)
            return repo_url
        owner = segments[0]
        allowed_owners = {
            o.strip()
            for o in os.environ.get(
                "SPARK_TOKEN_OWNERS",
                "perlowja,jperlow,nclawzero,ncz-os,mnemos-os",
            ).split(",")
            if o.strip()
        }
        if owner not in allowed_owners:
            logging.warning("refusing to attach token for non-fleet owner: %s", owner)
            return repo_url
        netloc = f"{username}:{token}@{host}"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))

    def _git(
        self, args: list[str], *, cwd: Path, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=self.git_timeout,
        )
        if check and proc.returncode != 0:
            raise RuntimeError(_redact_secrets(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}"))
        return proc

    def _repo_tree(self, repo_dir: Path) -> str:
        out = self._git(["ls-files"], cwd=repo_dir).stdout.splitlines()
        return "\n".join(out[:400])

    def _named_file_context(self, repo_dir: Path, task: str) -> str:
        names = set(re.findall(r"`([^`]+\.[A-Za-z0-9_./-]+)`", task))
        names.update(re.findall(r"\b[A-Za-z0-9_./-]+/[A-Za-z0-9_./-]+\.[A-Za-z0-9_./-]+\b", task))
        chunks = []
        for name in sorted(names)[:8]:
            path = (repo_dir / name).resolve()
            try:
                path.relative_to(repo_dir.resolve())
            except ValueError:
                continue
            if path.is_file() and path.stat().st_size <= 120_000:
                chunks.append(f"--- {name} ---\n{path.read_text(encoding='utf-8', errors='replace')[:20000]}")
        return "\n\n".join(chunks)

    def _extract_diff(self, text: str) -> str | None:
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```(?:diff)?\s*", "", stripped)
            stripped = re.sub(r"\s*```$", "", stripped).strip()
        diff_start = stripped.find("diff --git ")
        if diff_start < 0:
            diff_start = stripped.find("--- a/")
        if diff_start < 0:
            return None
        return stripped[diff_start:] + "\n"

    def _job_text(self, job: dict) -> str:
        parts = []
        for key in ("title", "description", "prompt"):
            if job.get(key):
                parts.append(str(job[key]))
        return "\n\n".join(parts)

    def _job_id_short(self, job: dict) -> str:
        raw = str(job.get("job_id") or job.get("id") or "manual")
        return re.sub(r"[^A-Za-z0-9._-]+", "-", raw)[:12] or "manual"

    def _commit_message(self, job: dict) -> str:
        task = " ".join(self._job_text(job).split())
        summary = task[:72].rstrip() or str(job.get("kind") or "Spark repo task")
        return f"{summary} (spark/nemotron, needs review)"


class DispatchingExecutor:
    def __init__(self, chat: Executor, repo: AgenticRepoExecutor):
        self.chat = chat
        self.repo = repo

    def execute(self, job: dict) -> dict:
        kind = str(job.get("kind") or "")
        if kind.startswith(NONCOMMIT_PREFIXES):
            return self.chat.execute(job)
        return self.repo.execute(job)


def _local_executor() -> OpenAIChatExecutor:
    return OpenAIChatExecutor(
        os.environ.get("LLM_BASE", "http://localhost:11434/v1"),
        os.environ.get("LLM_API_KEY", ""),
        os.environ.get("LLM_MODEL", "qwen2.5-coder:32b"),
        label="local",
        timeout=float(os.environ.get("LLM_TIMEOUT", "900")),
    )


def _ngc_executor() -> OpenAIChatExecutor:
    return OpenAIChatExecutor(
        os.environ.get("NGC_BASE", "https://integrate.api.nvidia.com/v1"),
        os.environ.get("NGC_API_KEY", ""),
        os.environ.get("NGC_MODEL", "nvidia/nvidia/nemotron-3-super-v3"),
        label="ngc",
        timeout=float(os.environ.get("NGC_TIMEOUT", "600")),
    )


def _make_chat_executor(name: str) -> Executor:
    if name == "local":
        return _local_executor()
    if name == "ngc":
        return _ngc_executor()
    if name in ("local+ngc", "auto"):  # local primary, NGC fallback
        return FallbackExecutor([("local", _local_executor()), ("ngc", _ngc_executor())])
    if name == "ngc+local":  # NGC primary, local fallback (operator 2026-06-04)
        return FallbackExecutor([("ngc", _ngc_executor()), ("local", _local_executor())])
    raise SystemExit(f"unknown executor {name!r}")


def make_executor(name: str) -> Executor:
    chat = _make_chat_executor(name)
    return DispatchingExecutor(chat, AgenticRepoExecutor(_ngc_executor()))


def _seal_terminal(uuid: str, payload: dict, key: bytes) -> bytes:
    return relay_crypto.seal(payload, key, aad=relay_crypto.aad_for("terminal", uuid))


def _write_terminal(
    relay: RelayClient, uuid: str, payload: dict, key: bytes, *, claimant: str | None = None
) -> None:
    # Echo the claimant URN through so the reconciler can PATCH the hive as the
    # job's claimant (the enqueuer that claimed it).
    if claimant and "claimant_urn" not in payload:
        payload = {**payload, "claimant_urn": claimant}
    if not relay.put_terminal(uuid, _seal_terminal(uuid, payload, key)):
        log.warning("terminal for %s already existed — keeping first", uuid)


def run_once(relay: RelayClient, key: bytes, executor: Executor, *, owner: str | None = None) -> int:
    """One sweep of pending/. Returns number of jobs executed this sweep."""
    owner = owner or worker_id()
    done = 0
    for uuid in relay.list_pending():
        if done >= MAX_PER_SWEEP:
            break
        if not relay.claim(uuid, owner):
            continue  # another live worker owns it (or lease not yet expired)
        try:
            job = relay_crypto.open_blob(
                relay.get_pending(uuid), key, aad=relay_crypto.aad_for("pending", uuid)
            )
        except relay_crypto.RelayCryptoError as exc:
            # Don't strand the claim: record a durable terminal failure so the
            # reconciler closes the job out instead of it blocking forever.
            log.exception("undecryptable pending %s — quarantining", uuid)
            _write_terminal(relay, uuid, {"status": "failed", "error": f"undecryptable pending: {exc}"}, key)
            done += 1
            continue
        claimant = job.get("claimant_urn")
        job.setdefault("job_id", uuid)
        if job.get("job_id") not in (None, uuid):
            log.error("payload job_id %r != object %s — quarantining", job.get("job_id"), uuid)
            _write_terminal(
                relay,
                uuid,
                {"status": "failed", "error": "job_id/object uuid mismatch"},
                key,
                claimant=claimant,
            )
            done += 1
            continue
        try:
            result = executor.execute(job)
            result.setdefault("status", "done")
            _write_terminal(relay, uuid, result, key, claimant=claimant)
            log.info("executed %s sha=%s", uuid, result.get("commit_sha"))
        except Exception as exc:  # noqa: BLE001 — report failure, keep polling
            log.exception("execute %s failed", uuid)
            _write_terminal(relay, uuid, {"status": "failed", "error": str(exc)}, key, claimant=claimant)
        done += 1
    return done


def main() -> None:
    ap = argparse.ArgumentParser(description="Spark relay poller (Spark side)")
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--executor", default="local+ngc", help="local | ngc | local+ngc (local primary)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument(
        "--worker-id",
        default=None,
        help="claim owner + identity; run several with distinct ids for concurrency",
    )
    ap.add_argument(
        "--report-gpu",
        action="store_true",
        help="also publish this host's GPU telemetry to the bucket (run on ONE worker only)",
    )
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    key = relay_crypto.load_key()
    relay = RelayClient()
    executor = make_executor(args.executor)
    owner = args.worker_id or worker_id()
    log.info("poller starting worker=%s executor=%s gpu=%s", owner, args.executor, args.report_gpu)

    if args.once:
        if args.report_gpu:
            report_gpu(relay, key)
        run_once(relay, key, executor, owner=owner)
        return

    while True:
        try:
            if args.report_gpu:
                report_gpu(relay, key)
            run_once(relay, key, executor, owner=owner)
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log.info("poller stopped")
            return
        except Exception:  # noqa: BLE001
            log.exception("poller loop error")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
