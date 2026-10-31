"""Mechanical validation of JSON config snippets in connector docs.

The ROADMAP entry "v4.1: smoke tests per surface where automatable"
is partially closed by the existing MCP-tool / dump-openapi /
namespace-isolation tests. This file closes the OTHER half:
parse every ```json``` fenced block in ``docs/connectors/*.md``
that looks like an MCP-server config, verify it parses, points
at a real ``mnemos`` subcommand, and uses one of the canonical
config shapes (stdio command/args, SSE transport+url).

Connector docs are operator-facing copy-paste material — a
broken example wastes operator time and is the kind of regression
that's easy to introduce when the CLI surface or transport
shape evolves. Mechanically asserting the snippets stay valid
catches the regression at CI time.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
CONNECTOR_DIR = REPO_ROOT / "docs" / "connectors"
# Skip the OpenAI-Custom-GPT doc — it's about an OpenAPI Actions
# spec, not an MCP server config; its JSON examples are
# fragments (servers[].url etc.), not full mcpServers blocks.
SKIPPED_DOCS = {"openai-custom-gpt.md", "README.md"}

# All MCP serve subcommand args we ship.
VALID_MNEMOS_SERVE_ARGS = {
    ("serve", "mcp-stdio"),
    ("serve", "mcp-http"),
}


def _connector_md_files() -> list[Path]:
    return [
        path for path in sorted(CONNECTOR_DIR.glob("*.md"))
        if path.name not in SKIPPED_DOCS
    ]


def _extract_json_blocks(text: str) -> list[str]:
    """Pull every ```json fenced block out of a markdown file."""
    pattern = re.compile(r"```json\n(.*?)```", re.DOTALL)
    return [match.group(1) for match in pattern.finditer(text)]


def _is_mcp_server_config(parsed: object) -> bool:
    """Recognise a top-level ``mcpServers`` block (Claude / Cursor /
    Continue / Cline shape). Some snippets are config FRAGMENTS
    (e.g., a single server entry without the wrapping
    ``mcpServers``) — those aren't validated here; only full
    config blocks are checked."""
    return (
        isinstance(parsed, dict)
        and isinstance(parsed.get("mcpServers"), dict)
    )


@pytest.mark.parametrize("doc_path", _connector_md_files(), ids=lambda p: p.name)
def test_connector_json_blocks_parse(doc_path: Path):
    """Every ```json``` fenced block in a connector doc must parse
    as valid JSON. Catches operator copy-paste-broken examples."""
    blocks = _extract_json_blocks(doc_path.read_text(encoding="utf-8"))
    if not blocks:
        pytest.skip(f"{doc_path.name} has no ```json``` blocks")
    failures: list[tuple[int, str]] = []
    for idx, block in enumerate(blocks):
        try:
            json.loads(block)
        except json.JSONDecodeError as exc:
            failures.append((idx, str(exc)))
    assert not failures, (
        f"{doc_path.name}: invalid JSON in fenced block(s): {failures!r}"
    )


def test_mcp_server_configs_use_real_subcommands():
    """Every ``mcpServers`` entry whose ``command`` is ``mnemos``
    must use a real ``mnemos`` subcommand path (``serve mcp-stdio``
    or ``serve mcp-http``). Catches CLI-shape drift in connector
    examples — common failure mode after a CLI restructure."""
    failures: list[tuple[str, str, str]] = []
    for doc in _connector_md_files():
        text = doc.read_text(encoding="utf-8")
        for block in _extract_json_blocks(text):
            try:
                parsed = json.loads(block)
            except json.JSONDecodeError:
                continue
            if not _is_mcp_server_config(parsed):
                continue
            for server_name, cfg in parsed["mcpServers"].items():
                if not isinstance(cfg, dict):
                    continue
                command = cfg.get("command")
                args = cfg.get("args", []) or []
                if command != "mnemos":
                    # Could be ssh, /absolute/path/to/mnemos, etc.
                    # The absolute-path case is intentional in
                    # claude-desktop.md. Skip non-bare-mnemos
                    # entries.
                    continue
                if not isinstance(args, list):
                    failures.append(
                        (doc.name, server_name, f"args must be list, got {type(args).__name__}")
                    )
                    continue
                shape: tuple = tuple(arg for arg in args if isinstance(arg, str))[:2]
                if shape not in VALID_MNEMOS_SERVE_ARGS:
                    failures.append(
                        (doc.name, server_name,
                         f"first 2 args {shape!r} not in {sorted(VALID_MNEMOS_SERVE_ARGS)!r}")
                    )
    assert not failures, (
        "connector doc mcpServers entry uses non-canonical mnemos "
        f"subcommand: {failures!r}"
    )


def test_mcp_server_configs_have_authorization_or_env_token():
    """Every ``mcpServers`` entry that runs ``mnemos serve mcp-stdio``
    must reference ``MNEMOS_API_KEY`` (env stamp), and every SSE entry
    must send a bearer ``Authorization`` header. Without it the
    connector example will 401 against a default-auth-enabled server.

    This used to collect violations into ``failures`` and then end
    without asserting anything, so it passed no matter what the docs
    said. Every scanned entry must now be compliant, and the test
    fails if it found nothing to scan (that would mean the extraction
    broke, not that the docs are clean).
    """
    failures: list[tuple[str, str, str]] = []
    scanned = 0
    for doc in _connector_md_files():
        text = doc.read_text(encoding="utf-8")
        for block in _extract_json_blocks(text):
            try:
                parsed = json.loads(block)
            except json.JSONDecodeError:
                continue
            if not _is_mcp_server_config(parsed):
                continue
            for server_name, cfg in parsed["mcpServers"].items():
                if not isinstance(cfg, dict):
                    continue
                command = cfg.get("command")
                args = cfg.get("args", []) or []
                # Stdio mnemos serve mcp-stdio path.
                if (
                    command == "mnemos"
                    and isinstance(args, list)
                    and len(args) >= 2
                    and args[:2] == ["serve", "mcp-stdio"]
                ):
                    scanned += 1
                    env = cfg.get("env") or {}
                    if not isinstance(env, dict) or "MNEMOS_API_KEY" not in env:
                        failures.append((doc.name, server_name, "stdio entry carries no MNEMOS_API_KEY"))
                # SSE transport variant.
                elif cfg.get("transport") == "sse":
                    scanned += 1
                    headers = cfg.get("headers") or {}
                    auth = headers.get("Authorization") or ""
                    if not auth.lower().startswith("bearer"):
                        failures.append((doc.name, server_name, "sse entry carries no bearer Authorization header"))

    assert scanned, "no mnemos serve mcp-stdio or SSE mcpServers entries were found in the connector docs"
    assert not failures, (
        "connector doc mcpServers entries have no credential, so the "
        f"copy-paste example 401s: {failures!r}"
    )


def test_at_least_one_doc_per_surface():
    """Connector gallery must have at least one Markdown per
    documented surface. Catches deletions / renames that leave the
    README's surface table pointing at nothing."""
    expected = {
        "claude-code.md",
        "claude-desktop.md",
        "cursor.md",
        "codex-cli.md",
        "continue.md",
        "cline.md",
        "chatgpt-pro-developer-mode.md",
        "openai-custom-gpt.md",
    }
    actual = {p.name for p in CONNECTOR_DIR.glob("*.md") if p.name != "README.md"}
    missing = expected - actual
    assert not missing, f"connector docs missing: {sorted(missing)}"


def test_readme_links_resolve():
    """The README's per-surface link list must point at files that
    actually exist."""
    readme = (CONNECTOR_DIR / "README.md").read_text(encoding="utf-8")
    pattern = re.compile(r"\]\(\.\/([a-z0-9-]+\.md)\)")
    failures = []
    for match in pattern.finditer(readme):
        target = CONNECTOR_DIR / match.group(1)
        if not target.exists():
            failures.append(match.group(1))
    assert not failures, f"README references missing files: {failures}"
