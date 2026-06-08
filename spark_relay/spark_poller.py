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


# Anchored to a STANDALONE final verdict line (the reviewer is told "End with
# exactly one line: VERDICT: …"). Matching only the last non-empty line means a
# VERDICT-like string quoted from the diff/task body cannot flip the result.
# End-anchored: the final line must be ONLY the verdict — "VERDICT: APPROVE but
# I have concerns" must NOT count as a clean approval (fail closed to 'error').
_VERDICT_LINE_RE = re.compile(r"^VERDICT:\s*(NEEDS[- ]ATTENTION|APPROVE)\s*$", re.I)


def _classify_review_verdict(txt: str) -> dict:
    """Parse a reviewer reply into a verdict, fail-CLOSED on ambiguity.

    Only the FINAL non-empty line is considered, and it must be a clean
    ``VERDICT: APPROVE`` / ``VERDICT: NEEDS-ATTENTION`` line. A verdict-like
    string quoted in the body (e.g. the reviewer citing the diff, which may
    contain the literal instruction text) is ignored. Empty / verdict-less /
    trailing-prose output → ``error``, NOT a silent approve, so a broken or
    evasive reviewer never green-lights a patch. The reconciler lands 'error'
    (flagged unreviewed — still a review branch a human must merge) but BLOCKS
    'needs-attention'."""
    lines = [ln.strip() for ln in (txt or "").splitlines() if ln.strip()]
    if not lines:
        return {"verdict": "error", "text": "empty reviewer output"}
    m = _VERDICT_LINE_RE.match(lines[-1])
    if not m:
        return {"verdict": "error", "text": (txt or "").strip()[:1500]}
    last = m.group(1).upper().replace(" ", "-")
    verdict = "needs-attention" if last == "NEEDS-ATTENTION" else "approve"
    return {"verdict": verdict, "text": (txt or "").strip()[:1500]}


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
        # Request a LARGE output budget. The agentic flow asks the model to
        # return the COMPLETE new content of each changed file; with no
        # max_tokens the provider default (often ~4k) truncates big files
        # before the closing ===END=== marker, so _extract_file_blocks finds
        # nothing and the job fails "no applicable file changes" — even though
        # the model produced correct output (observed 2026-06-08: claude-opus
        # truncated mid-_entity_match.py on 3 riskyeats jobs). 32k covers any
        # single source file; cap it under the model's context window.
        max_out = int(os.environ.get("SPARK_MAX_OUTPUT_TOKENS", "32000"))
        resp = requests.post(
            f"{self.base}/chat/completions",
            headers=headers,
            json={
                "model": model or self.default_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_out,
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
            # Full-file replacement flow (2026-06-06). The previous unified-diff
            # flow was diff-blind: the model only ever saw the repo tree plus a
            # few excerpts, then had to emit byte-exact context hunks — every
            # relayed job failed `git apply --check` (fabricated index hashes,
            # corrupt patches), retries included. Instead: (1) the model picks
            # the files it needs, (2) we feed it their FULL content, (3) it
            # returns complete replacement files, (4) git computes the real
            # diff from the written tree.
            wanted = self._request_file_list(job, tree)
            file_ctx = self._full_files_context(clone_dir, wanted)
            retries = int(os.environ.get("SPARK_DIFF_RETRIES", "2"))
            model_text = ""
            feedback = ""
            changes: dict[str, str | None] = {}
            for _attempt in range(retries + 1):
                model_text = self._request_changes(job, tree, file_ctx, feedback=feedback)
                changes = self._extract_file_blocks(model_text)
                if not changes:
                    feedback = (
                        "your previous reply contained no valid ===FILE: path=== ... ===END=== "
                        "blocks; output the COMPLETE new content of every file you change"
                    )
                    continue
                # Reset any prior attempt's writes FIRST — the guards below
                # must compare against pristine HEAD, not a previous attempt's
                # already-mangled tree (reset used to run after the truncation
                # check, so attempt 2 measured against attempt 1's output).
                self._git(["checkout", "--", "."], cwd=clone_dir, check=False)
                self._git(["clean", "-fdq"], cwd=clone_dir, check=False)
                # Truncation guard (2026-06-06): a "replacement" much smaller
                # than the original is an output-budget overflow, not an edit —
                # stonkmode.py went 679 -> 133 lines and lost 546 lines of
                # working code. Reject before writing anything.
                trunc = self._truncation_violations(clone_dir, changes)
                if trunc:
                    feedback = (
                        "you TRUNCATED these files (returned far less than the original "
                        f"content): {'; '.join(trunc)}. Output the COMPLETE new file "
                        "content for every file you change — every original line that "
                        "you are not deliberately changing must be preserved verbatim."
                    )
                    continue
                # Interface guard (2026-06-06): replacements must not silently
                # drop top-level def/class names — one patch deleted
                # bad_actors.load(), the module's import entrypoint, while
                # passing the size ratio.
                dropped = self._dropped_symbols(clone_dir, changes)
                if dropped:
                    feedback = (
                        "your replacement files REMOVED these top-level functions/classes "
                        f"that other code may import: {'; '.join(dropped)}. Keep every "
                        "existing top-level def/class (you may modify their bodies) and "
                        "output the COMPLETE corrected files."
                    )
                    continue
                bad = self._write_file_changes(clone_dir, changes)
                if bad:
                    return {
                        "status": "needs-review",
                        "error": f"unsafe paths rejected: {bad}",
                        "suggestion": model_text,
                        "repo": repo_url,
                    }
                # Compile gate (2026-06-06): replaced .py files must at least
                # parse — the same incident shipped two syntax errors.
                pyerr = self._compile_failures(clone_dir, changes)
                if pyerr:
                    feedback = (
                        "your replacement files have Python syntax errors: "
                        f"{'; '.join(pyerr)}. Output corrected COMPLETE file content."
                    )
                    continue
                break
            else:
                return {
                    "status": "needs-review",
                    "error": "model produced no applicable file changes",
                    "attempts": retries + 1,
                    "last_feedback": feedback[:500],
                    "suggestion": model_text,
                    "repo": repo_url,
                }
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
                    "error": "model file replacements were identical to existing content",
                    "suggestion": model_text,
                    "repo": repo_url,
                }
            # MANDATORY cross-family adversarial review (operator 2026-06-08):
            # the authoring model must NOT review its own output. Claude-authored
            # code is reviewed by codex on EIH (and vice-versa). Catches the
            # failure class that slipped through before max_tokens was enough to
            # land: reverting unrelated merged work + dead/unwired code (e.g. a
            # haversine helper added but never called). The verdict rides in the
            # result; the PYTHIA lander refuses to land a needs-attention patch.
            staged_diff = self._git(["diff", "--cached"], cwd=clone_dir).stdout
            review = self._adversarial_review(job, staged_diff)
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
                "adversarial_review": review,
                "metrics": {"backend": "ngc-agentic", "model": self._agentic_model() or self.chat.default_model},
            }
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _chat_suggestion(self, job: dict) -> str:
        try:
            return self.chat.execute(job)["metrics"]["output"]
        except Exception as exc:  # noqa: BLE001
            return f"chat suggestion failed: {exc}"

    def _agentic_model(self) -> str | None:
        return os.environ.get("SPARK_AGENTIC_MODEL") or None

    def _request_file_list(self, job: dict, tree: str) -> list[str]:
        """Phase 1: the model names the files it needs to read/change."""
        max_files = int(os.environ.get("SPARK_MAX_CONTEXT_FILES", "8"))
        system = (
            "You are a repository editing agent planning a change. Reply with ONLY "
            "the repo-relative paths of the files you need to read or modify, one "
            f"per line, at most {max_files}. No prose, no fences."
        )
        user = f"Repo tree:\n{tree}\n\nTask:\n{self._job_text(job)}"
        out = self.chat.complete(
            system=system, user=user, model=self._agentic_model(), timeout=self.model_timeout
        )
        paths = []
        for line in out.splitlines():
            line = line.strip().strip("`").lstrip("-* ").strip()
            if line and "/" in line or (line and "." in line):
                paths.append(line)
        return paths[:max_files]

    def _full_files_context(self, clone_dir: Path, paths: list[str]) -> str:
        """Phase 2 input: FULL content of the requested files (size-capped)."""
        cap = int(os.environ.get("SPARK_FILE_CAP", "40000"))
        parts = []
        for rel in paths:
            p = (clone_dir / rel).resolve()
            try:
                p.relative_to(clone_dir.resolve())
            except ValueError:
                continue  # traversal attempt — skip
            if not p.is_file():
                parts.append(f"===FILE: {rel}===\n(does not exist — you may create it)\n===END===")
                continue
            try:
                body = p.read_text(encoding="utf-8", errors="replace")[:cap]
            except Exception:  # noqa: BLE001
                continue
            parts.append(f"===FILE: {rel}===\n{body}\n===END===")
        return "\n\n".join(parts)

    def _request_changes(self, job: dict, tree: str, file_ctx: str, *, feedback: str = "") -> str:
        """Phase 2: the model returns COMPLETE replacement files."""
        system = (
            "You are a repository editing agent. You are given the full current "
            "content of the relevant files. Implement the task by outputting the "
            "COMPLETE new content of every file you change or create, using exactly "
            "this framing for each file:\n"
            "===FILE: relative/path===\n<entire new file content>\n===END===\n"
            "To delete a file output: ===DELETE: relative/path===\n"
            "Output ONLY these blocks. No prose, no markdown fences, no diffs."
        )
        user = (
            f"Repo tree:\n{tree}\n\n"
            f"Current file contents:\n{file_ctx or '(no files selected)'}\n\n"
            f"Task:\n{self._job_text(job)}"
        )
        if feedback:
            user += f"\n\nCorrection: {feedback}"
        return self.chat.complete(
            system=system, user=user, model=self._agentic_model(), timeout=self.model_timeout
        )

    # Tolerant framing (2026-06-06): models drop the trailing === on the FILE
    # header (an 8KB perfectly-good module was discarded as "no applicable
    # file changes"). Accept optional leading whitespace, optional trailing
    # ===, and whitespace around markers.
    _FILE_BLOCK_RE = re.compile(
        r"^[ \t]*===[ \t]*FILE:[ \t]*(?P<path>[^\n=]+?)[ \t]*(?:===)?[ \t]*\n"
        r"(?P<body>.*?)\n?[ \t]*===[ \t]*END[ \t]*===",
        re.S | re.M,
    )
    _DELETE_BLOCK_RE = re.compile(
        r"^[ \t]*===[ \t]*DELETE:[ \t]*(?P<path>[^\n=]+?)[ \t]*(?:===)?[ \t]*$", re.M
    )

    def _extract_file_blocks(self, text: str) -> dict[str, str | None]:
        """Parse model output into {relpath: new_content | None-to-delete}."""
        changes: dict[str, str | None] = {}
        for m in self._FILE_BLOCK_RE.finditer(text or ""):
            changes[m.group("path").strip()] = m.group("body")
        for m in self._DELETE_BLOCK_RE.finditer(text or ""):
            changes[m.group("path").strip()] = None
        return changes

    def _truncation_violations(self, clone_dir: Path, changes: dict[str, str | None]) -> list[str]:
        """Replacements for EXISTING files must keep >= SPARK_MIN_REPLACEMENT_RATIO
        of the original line count (default 0.6). Deletions and new files pass."""
        ratio = float(os.environ.get("SPARK_MIN_REPLACEMENT_RATIO", "0.6"))
        out = []
        for rel, body in changes.items():
            if body is None:
                continue  # explicit delete
            p = clone_dir / rel
            if not p.is_file():
                continue  # new file
            try:
                orig_lines = p.read_text(encoding="utf-8", errors="replace").count("\n") or 1
            except Exception:  # noqa: BLE001
                continue
            new_lines = body.count("\n") or 1
            if orig_lines >= 40 and new_lines < orig_lines * ratio:
                out.append(f"{rel} (original {orig_lines} lines, you returned {new_lines})")
        return out

    _TOPLEVEL_DEF_RE = re.compile(r"^(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)

    def _dropped_symbols(self, clone_dir: Path, changes: dict[str, str | None]) -> list[str]:
        """Top-level def/class names present in the original .py but absent
        from its replacement. Deletions and new files pass."""
        out = []
        for rel, body in changes.items():
            if body is None or not rel.endswith(".py"):
                continue
            p = clone_dir / rel
            if not p.is_file():
                continue
            try:
                orig = p.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
            old_syms = set(self._TOPLEVEL_DEF_RE.findall(orig))
            new_syms = set(self._TOPLEVEL_DEF_RE.findall(body))
            missing = sorted(old_syms - new_syms)
            if missing:
                out.append(f"{rel}: {', '.join(missing[:6])}")
        return out

    def _compile_failures(self, clone_dir: Path, changes: dict[str, str | None]) -> list[str]:
        """py_compile every replaced/created .py file; return error summaries."""
        out = []
        for rel, body in changes.items():
            if body is None or not rel.endswith(".py"):
                continue
            p = clone_dir / rel
            if not p.is_file():
                continue
            proc = subprocess.run(
                ["python3", "-m", "py_compile", str(p)],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout).strip().splitlines()
                out.append(f"{rel}: {err[-1][:200] if err else 'compile failed'}")
        return out

    def _write_file_changes(self, clone_dir: Path, changes: dict[str, str | None]) -> list[str]:
        """Write replacements into the clone. Returns rejected (unsafe) paths."""
        bad = []
        root = clone_dir.resolve()
        for rel, body in changes.items():
            if rel.startswith(("/", "~")) or ".." in Path(rel).parts or rel.startswith(".git/"):
                bad.append(rel)
                continue
            p = (clone_dir / rel).resolve()
            try:
                p.relative_to(root)
            except ValueError:
                bad.append(rel)
                continue
            if body is None:
                p.unlink(missing_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                if not body.endswith("\n"):
                    body += "\n"
                p.write_text(body, encoding="utf-8")
        return bad

    def _resolve_repo_url(self, job: dict) -> str | None:
        # A repo: hint that doesn't resolve falls THROUGH to the kind map —
        # the old early-return turned an unknown hint into "no repo mapping"
        # even when the kind was mapped (2026-06-06).
        hint = REPO_HINT_RE.search(self._job_text(job))
        if hint:
            url = self._repo_hint_to_url(hint.group("repo"))
            if url:
                return url
        kind = str(job.get("kind") or "")
        for prefixes, url in KIND_WORKSPACE_MAP:
            if kind.startswith(prefixes):
                return url
        return None

    def _repo_hint_to_url(self, repo: str) -> str | None:
        # Spark scope (operator override 2026-06-06): Spark may work ANY fleet
        # project — its NGC pool is large and non-metered. The previous
        # "open source + NVIDIA-internal only" exclusion of argonautsystems
        # projects is lifted.
        aliases = {
            "mnemos": "https://gitlab.com/mnemos-os/mnemos.git",
            "zeroclaw": "https://gitlab.com/nclawzero/zeroclaw.git",
            "ncz-installer": "https://gitlab.com/nclawzero/ncz-installer.git",
            "riskyeats": "https://gitlab.com/perlowja/riskyeats.git",
            "ic-engine": "https://gitlab.com/argonautsystems/ic-engine.git",
            "investorclaw-enterprise": "https://gitlab.com/argonautsystems/InvestorClaw.git",
            "florida-licenses": "https://gitlab.com/argonautsystems/florida-licenses.git",
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
                "perlowja,jperlow,nclawzero,ncz-os,mnemos-os,argonautsystems",
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

    def _adversarial_reviewer_model(self) -> str:
        """Cross-family reviewer: a model never reviews its own family's output.
        Author=claude -> reviewer=codex (default). Author=gpt/codex -> reviewer=claude."""
        author = (self._agentic_model() or self.chat.default_model or "").lower()
        # OpenAI family incl. o-series (o3/o4-mini/...) → review with a non-OpenAI
        # model so a family never reviews its own output.
        openai_family = bool(
            "codex" in author or "gpt" in author or "openai" in author
            or re.search(r"(^|[/_-])o[0-9]", author)
        )
        if openai_family:
            return os.environ.get("SPARK_REVIEW_MODEL_ALT", "azure/anthropic/claude-opus-4-8")
        return os.environ.get("SPARK_REVIEW_MODEL", "azure/openai/gpt-5.3-codex")

    def _adversarial_review(self, job: dict, diff: str) -> dict:
        """Adversarial review of the staged diff via a cross-family model on EIH.

        Returns ``{verdict: 'approve'|'needs-attention'|'error', model, text}``.
        verdict='needs-attention' tells the PYTHIA lander to REFUSE to land
        (the patch is preserved for human review). A reviewer infra error fails
        OPEN to verdict='error' (lands, but flagged unreviewed) so a codex
        outage does not strand all spark work — only a CLEAR rejection blocks."""
        model = self._adversarial_reviewer_model()
        # A diff the reviewer cannot see in FULL cannot be approved — a patch
        # could hide a revert / dead code past a truncation point and still get
        # APPROVE on the visible prefix. Frontier reviewers take large context;
        # if the diff still exceeds the budget, BLOCK (needs-attention) so a
        # human reviews it, rather than reviewing a truncated view.
        review_cap = int(os.environ.get("SPARK_REVIEW_DIFF_CHARS", "200000"))
        if len(diff or "") > review_cap:
            return {
                "verdict": "needs-attention",
                "model": model,
                "text": f"diff {len(diff)} chars exceeds review budget {review_cap} — blocked for human review",
            }
        system = (
            "You are an adversarial code reviewer. The diff must implement EXACTLY the task and "
            "nothing more. Reply NEEDS-ATTENTION if it: reverts or removes unrelated existing code, "
            "adds dead/unwired code (a function/method never called), changes constants/thresholds "
            "outside the task scope, or is incorrect. Otherwise APPROVE. End with exactly one line: "
            "VERDICT: APPROVE or VERDICT: NEEDS-ATTENTION."
        )
        # Full diff (size already gated above) so the reviewer sees every change.
        user = f"TASK:\n{self._job_text(job)[:4000]}\n\nDIFF:\n{diff or ''}"
        try:
            import requests  # import inside try: an import failure must not break execute()
            headers = {"Authorization": f"Bearer {self.chat.api_key}"} if self.chat.api_key else {}
            if "codex" in model.lower():
                resp = requests.post(
                    f"{self.chat.base}/responses",
                    headers=headers,
                    json={"model": model, "max_output_tokens": 1500,
                          "instructions": system, "input": user},
                    timeout=self.model_timeout,
                )
                resp.raise_for_status()
                txt = ""
                for o in resp.json().get("output", []):
                    if o.get("type") == "message":
                        for c in o.get("content", []):
                            if c.get("type") == "output_text":
                                txt += c.get("text", "")
            else:
                txt = self.chat.complete(system=system, user=user, model=model, timeout=self.model_timeout)
            return {"model": model, **_classify_review_verdict(txt)}
        except Exception as exc:  # noqa: BLE001 — infra error fails OPEN (flagged), never breaks execute()
            return {"verdict": "error", "model": model, "text": f"reviewer error (fail-open): {exc}"[:300]}

    def _commit_message(self, job: dict) -> str:
        task = " ".join(self._job_text(job).split())
        summary = task[:72].rstrip() or str(job.get("kind") or "Spark repo task")
        # Label with the ACTUAL model, not a hardcoded "nemotron" (misleading —
        # nemotron is retired from spark code-gen; the agentic model is frontier
        # claude/gpt or the local Qwen3-Coder-Next per SPARK_AGENTIC_MODEL/NGC_MODEL).
        model = self._agentic_model() or os.environ.get("NGC_MODEL") or "spark"
        model_tag = re.sub(r"^.*/", "", str(model))[:40]
        return f"{summary} (spark/{model_tag}, needs review)"


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
            raw = relay.get_pending(uuid)
        except Exception as exc:  # noqa: BLE001 — stale claim marker for a
            # pending object that was already consumed/deleted (GCS 404).
            # Previously this propagated and KILLED the poller process
            # (2026-06-06); skip and keep sweeping.
            log.warning("pending object for %s unreadable (%s) — skipping", uuid, exc)
            continue
        try:
            job = relay_crypto.open_blob(
                raw, key, aad=relay_crypto.aad_for("pending", uuid)
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
