"""Guard: no private-network address literals introduced into shipped files.

The product default for the HTTP embed and rerank endpoints must be *empty*
— operators point it at an endpoint they trust. This test fails if a
private (non-routable) address literal is reintroduced into the config,
code, docs, and default-env files this repository ships, or if any provider
in ``data/llm_provider_registry.json`` gains a private-range base URL.

Ranges treated as private / non-routable:

- RFC 1918 blocks A, B and C
- RFC 6598 CGNAT
- RFC 3927 link-local

Loopback (``127.0.0.1``) and the wildcard (``0.0.0.0``) are deliberately
allowed: they are the standard way to document a local endpoint and say
nothing about anyone's infrastructure.

**Baseline.** Some private literals predate this change and are out of
scope. ``tests/private_literal_baseline.json`` records, per file, how
many private literals it already carried, and which registry fields already
pointed at a private host. It holds counts and field names only, never an
address. A file that gains a literal beyond its count, a file not in the
baseline that gains one, or a newly private registry field fails this test.
The check needs no git history, so shallow CI clones behave the same.

If a private endpoint is legitimately needed, express it as an environment
variable with an empty default (see ``MNEMOS_EMBED_HTTP_URL`` and
``MNEMOS_RERANKER_URL``) rather than hardcoding the address.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]

# Any dotted-quad IPv4 literal in a non-routable range. Word-bounded so a
# version string or a longer digit run cannot produce a false match.
_PRIVATE_IP_RE = re.compile(
    r"(?<![\w.])"
    r"(?:"
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}"
    r"|169\.254\.\d{1,3}\.\d{1,3}"
    r")"
    r"(?![\w.])"
)

# ``scheme://user:pass@host:port`` — captured so the registry check can see
# the host portion of a provider base URL.
_URL_HOST_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://(?:[^@/]*@)?\[?([^\]/:?#]+)\]?")

# Historical records: frozen docs that document past architecture.
_HISTORICAL_MD_NAMES = frozenset(
    {
        "CHANGELOG.md",
        "EVOLUTION.md",
        "FIX_REPORT_2026-09-09.md",
        "FIX_REPORT_ROUND2_2026-09-09.md",
        "KNOWN_LIMITATIONS.md",
    }
)

# Binary / generated artifacts that are not human-authored config.
_SKIP_SUFFIXES = frozenset(
    {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".pdf", ".gz", ".so", ".whl"}
)

# Directories that are never part of the shipped tree.
_SKIP_DIRS = frozenset({".git", "build", "dist", "node_modules", ".venv", ".venv-ci", ".pytest_cache", "__pycache__"})

# Test files that legitimately exercise private-host handling. They are
# unit-test fixtures (e.g. a ``PG_HOST`` in the RFC 1918 range to assert a
# remote host is treated as remote), not product defaults.
_TEST_FIXTURE_FILES = frozenset(
    {
        "tests/test_postgres_embedding_dim.py",
        "tests/test_installer_embedding_dim.py",
    }
)

# Module that parses/validates operator-supplied hosts; it only names a
# private address in a comment explaining the remote-host case.
_CODE_COMMENT_FILES = frozenset(
    {
        "mnemos/installer/__main__.py",
        "mnemos/installer/db.py",
    }
)


_BASELINE_PATH = REPO / "tests" / "private_literal_baseline.json"


def _baseline() -> dict:
    return json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))


def _private_literals_in(text: str) -> list[tuple[int, str]]:
    """Every private literal, all of them on each line."""
    return [
        (lineno, m.group(0))
        for lineno, line in enumerate(text.splitlines(), start=1)
        for m in _PRIVATE_IP_RE.finditer(line)
    ]


def _is_historical_doc(path: Path) -> bool:
    if "docs" in path.parts and "history" in path.parts:
        return True
    return path.suffix == ".md" and path.name in _HISTORICAL_MD_NAMES


def _candidate_files() -> list[Path]:
    """Tracked files when this is a git checkout, else every file on disk."""
    proc = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0 and proc.stdout:
        return sorted(REPO / rel for rel in proc.stdout.split("\0") if rel)
    return sorted(REPO.rglob("*"))


def _iter_live_files() -> list[Path]:
    """Every shipped text file, minus frozen history, generated artifacts,
    and this test itself."""
    self_path = Path(__file__).resolve()
    out: list[Path] = []
    for path in _candidate_files():
        if not path.is_file():
            continue
        if path.resolve() in (self_path, _BASELINE_PATH.resolve()):
            continue
        if any(p in _SKIP_DIRS for p in path.parts):
            continue
        if "egg-info" in path.parts:
            continue
        if path.suffix in _SKIP_SUFFIXES:
            continue
        if _is_historical_doc(path):
            continue
        out.append(path)
    return out


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def _live_counts() -> dict[str, list[tuple[int, str]]]:
    out: dict[str, list[tuple[int, str]]] = {}
    for path in _iter_live_files():
        rel = _rel(path)
        if rel in _TEST_FIXTURE_FILES or rel in _CODE_COMMENT_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        found = _private_literals_in(text)
        if found:
            out[rel] = found
    return out


def test_private_ip_literal_in_live_files():
    """No shipped file may *gain* a private-range IP literal.

    Reintroduce a private-range address anywhere in live config, code, docs
    or default-env templates and this test fails with the locus. Files that
    already carried literals may keep that many (see module docstring).
    """
    allowed = _baseline()["files"]
    offenders: list[str] = []
    for rel, found in _live_counts().items():
        if len(found) > allowed.get(rel, 0):
            where = ", ".join(f"line {ln}" for ln, _lit in found)
            offenders.append(f"  {rel}: {len(found)} private literal(s), baseline {allowed.get(rel, 0)} ({where})")
    assert not offenders, (
        "live files gained private-network IP literals; use an env var with an empty default instead:\n"
        + "\n".join(offenders)
    )


def test_baseline_holds_counts_only():
    """The baseline must never publish an address, and a path it does not
    list has no allowance."""
    text = _BASELINE_PATH.read_text(encoding="utf-8")
    assert _PRIVATE_IP_RE.search(text) is None
    data = json.loads(text)
    assert all(isinstance(v, int) and v > 0 for v in data["files"].values())
    assert data["files"].get("totally_new_file.py", 0) == 0


def test_baseline_has_no_slack():
    """A file that dropped literals must lower its baseline count, so the
    allowance can never be reused for a new address."""
    live = {rel: len(found) for rel, found in _live_counts().items()}
    stale = {rel: n for rel, n in _baseline()["files"].items() if live.get(rel, 0) < n}
    assert not stale, f"lower these baseline counts to the live values {live}: {stale}"


def test_no_new_private_literal_in_endpoint_defaults():
    """The embed/rerank endpoint defaults must be empty — no private
    address may be baked into the module-level defaults or env readers."""
    from mnemos.core import config as core_config
    from mnemos.domain.search import reranker as reranker_mod
    from mnemos.runtime import embedder as embedder_mod

    defaults = {
        "embedder.DEFAULT_HTTP_URL": embedder_mod.DEFAULT_HTTP_URL,
        "embedder.DEFAULT_HTTP_URL_FALLBACK": embedder_mod.DEFAULT_HTTP_URL_FALLBACK,
        "reranker.DEFAULT_RERANKER_URL": reranker_mod.DEFAULT_RERANKER_URL,
    }
    for name, value in defaults.items():
        assert _PRIVATE_IP_RE.search(value or "") is None, f"{name} defaults to a private address: {value!r}"

    for fn_name in ("embed_http_url_fallback_env", "reranker_url_env"):
        fn = getattr(core_config, fn_name)
        value = fn()
        assert _PRIVATE_IP_RE.search(value or "") is None, f"{fn_name}() defaults to a private address: {value!r}"


def test_default_env_files_have_no_private_endpoint():
    """The endpoint env templates this repo ships must not default to a
    private address."""
    env_files = [p for p in _iter_live_files() if p.name == ".env.example" or p.name.endswith(".env.example")]
    assert env_files, "expected at least one shipped .env.example template"
    offenders: list[str] = []
    for path in env_files:
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            m = _PRIVATE_IP_RE.search(value)
            if m:
                offenders.append(f"  {_rel(path)}:{lineno}: {key.strip()}={m.group(0)!r}")
    assert not offenders, "env templates default to a private endpoint:\n" + "\n".join(offenders)


def _private_host(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    m = _URL_HOST_RE.match(value.strip())
    host = (m.group(1) if m else value.strip()).strip()
    return host if _PRIVATE_IP_RE.fullmatch(host) else None


def test_provider_registry_has_no_private_base_url():
    """No provider in ``data/llm_provider_registry.json`` may gain a
    private-range base URL. Fields that already pointed at a private host
    are listed by name in the baseline; any other private base URL fails.
    """
    registry_path = REPO / "data" / "llm_provider_registry.json"
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    providers = data.get("providers", {})
    assert providers, "provider registry must define providers"

    allowed = set(_baseline()["registry_private_fields"])
    offenders: list[str] = []
    for name, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        for key in ("base_url", "anthropic_base_url"):
            field = f"{name}.{key}"
            if _private_host(provider.get(key, "")) and field not in allowed:
                offenders.append(f"  providers.{field}")
    assert not offenders, "provider registry advertises a private-range base URL:\n" + "\n".join(offenders)


def test_provider_registry_has_no_cloud_default_for_embeddings_rerank():
    """The registry must never designate a cloud provider as the default
    primary for the ``embeddings`` or ``rerank`` task routes. Memory text
    must not be sent to a third party by default; cloud models are opt-in
    via tier lists (``B_embedding`` / ``B_rerank``) only."""
    data = json.loads((REPO / "data" / "llm_provider_registry.json").read_text(encoding="utf-8"))
    routing = data.get("use_case_routing", {})

    for key in ("embeddings", "rerank"):
        assert key not in routing, (
            f"use_case_routing.{key!r} is present; cloud providers must not be "
            f"the default for {key} (memory text must not go to a third party by default)"
        )

    tier_picks = data["tier_picks"]
    assert "B_embedding" in tier_picks and tier_picks["B_embedding"]
    assert "B_rerank" in tier_picks and tier_picks["B_rerank"]


def test_provider_registry_policy_audit_is_present_and_neutral():
    """The registry carries an in-file ``_policy_audit`` block documenting
    the decision to keep ``embeddings`` and ``rerank`` out of
    ``use_case_routing``. The block must be present and must not name
    internal hosts, jobs, or reviewers."""
    data = json.loads((REPO / "data" / "llm_provider_registry.json").read_text(encoding="utf-8"))

    audit = data.get("_policy_audit")
    assert audit is not None, "registry must carry a top-level _policy_audit field"
    assert audit.get("last_review") == "2026-10-17", (
        f"_policy_audit.last_review should be 2026-10-17, got {audit.get('last_review')!r}"
    )
    assert audit.get("reviewer") == "policy-review", (
        f"_policy_audit.reviewer must be the neutral value 'policy-review', got {audit.get('reviewer')!r}"
    )

    decisions = " ".join(audit.get("decisions", []))
    for forbidden_key in ("embeddings", "rerank"):
        assert forbidden_key in decisions, (
            f"_policy_audit.decisions must explicitly name the {forbidden_key!r} "
            f"task-route key as forbidden from cloud defaults; got: {decisions!r}"
        )

    # The audit must match reality: those two keys are still absent.
    routing = data["use_case_routing"]
    assert "embeddings" not in routing
    assert "rerank" not in routing


def test_llm_pick_loader_tolerates_absent_embeddings_rerank_keys():
    """Removing the task-route entries must not break the registry loader.

    ``data/llm-pick.sh`` is the only consumer. It resolves task routes with
    ``d['use_case_routing'].get(...)`` and lists keys with ``.keys()``, so an
    absent key degrades to a friendly "no routing" message rather than a
    KeyError. This test locks that tolerance in: it asserts the shipped
    script contains no direct subscript of either removed key, and that the
    shipped registry actually omits them.
    """
    script = (REPO / "data" / "llm-pick.sh").read_text(encoding="utf-8")

    assert "use_case_routing']['embeddings']" not in script
    assert "use_case_routing']['rerank']" not in script
    assert 'use_case_routing\']["embeddings"]' not in script
    assert 'use_case_routing\']["rerank"]' not in script

    assert "use_case_routing'].get(" in script
    assert "use_case_routing'].keys()" in script

    data = json.loads((REPO / "data" / "llm_provider_registry.json").read_text(encoding="utf-8"))
    routing = data["use_case_routing"]
    assert "embeddings" not in routing
    assert "rerank" not in routing
    assert routing.get("zeroclaw_fanout_routine", {}).get("primary")


def test_compose_embed_host_points_at_compose_ollama_service():
    """``INFERENCE_EMBED_HOST`` must address the ``ollama`` compose service.

    ``localhost`` is wrong inside the mnemos container — it resolves to the
    container's own loopback. ``ollama`` is a service defined in this same
    compose file, so it is a legitimate in-network target.
    """
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert "ollama" in services, "the compose file must define the ollama service it points at"

    env = services["mnemos"]["environment"]
    host = env["INFERENCE_EMBED_HOST"]
    assert host == "http://ollama:11434", f"INFERENCE_EMBED_HOST must be the compose ollama service, got {host!r}"
    assert not host.startswith("http://localhost"), "localhost inside the mnemos container is not the ollama service"


def test_env_example_does_not_demonstrate_container_localhost():
    """.env.example must not teach operators the container-localhost bug."""
    example = (REPO / ".env.example").read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in example.splitlines()
        if line.strip().startswith("INFERENCE_EMBED_HOST=") and "localhost" in line
    ]
    assert not offenders, (
        f".env.example documents INFERENCE_EMBED_HOST as container-localhost: {offenders}; "
        "inside the mnemos container that resolves to the container's own loopback"
    )


def test_pantheon_vip_caddyfile_removed():
    """The retired PANTHEON VIP reverse-proxy snippet must not be present."""
    snippet = REPO / "deploy" / "pantheon" / "Caddyfile.pantheon-vip-4100.snippet"
    assert not snippet.exists(), "Caddyfile.pantheon-vip-4100.snippet was re-added; the VIP cutover is retired."


def test_live_nvidia_endpoints_kept():
    """Sanity: the public NVIDIA endpoints must still be present so this
    test cannot be satisfied by blanketing the registry."""
    blob = (REPO / "data" / "llm_provider_registry.json").read_text(encoding="utf-8")
    assert "https://inference-api.nvidia.com" in blob
    assert "https://integrate.api.nvidia.com" in blob


@pytest.mark.parametrize(
    "address",
    [
        ".".join(("10", "1", "2", "3")),
        ".".join(("172", "16", "0", "9")),
        ".".join(("172", "31", "255", "254")),
        ".".join(("192", "168", "0", "1")),
        ".".join(("100", "64", "0", "7")),
        ".".join(("169", "254", "1", "1")),
    ],
)
def test_private_ip_regex_matches_private_ranges(address: str):
    """The guard's detector must actually catch each non-routable range.

    Addresses are assembled from their octets so that this test file does
    not itself contain the literals it guards against.
    """
    assert _PRIVATE_IP_RE.search(f"base_url = http://{address}:8080/v1") is not None


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "0.0.0.0",
        ".".join(("8", "8", "8", "8")),
        "172.32.0.1",
        "11.0.0.1",
        "192.169.0.1",
        "100.128.0.1",
    ],
)
def test_private_ip_regex_ignores_routable_and_boundary_ranges(address: str):
    """Boundary addresses just outside every blocked range must not match
    (loopback and wildcard are explicitly allowed)."""
    assert _PRIVATE_IP_RE.search(f"http://{address}:8080/v1") is None
