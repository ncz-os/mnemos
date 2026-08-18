"""Slice #193: pin doc "current" version claims to the actual
package version.

Surfaced by the deep documentation-sweep codex audit at HEAD
``de13b51`` (mem_1778221719446_2cdcad in MNEMOS), which found
~10 documentation files still claiming "current is v5.0.0" or
"current v4.0.0 release line" while ``pyproject.toml`` and
``mnemos/_version.py`` were both at v5.3.2.

This test pins the live current-version claim across the
operator-facing docs. Historical mentions (e.g. "v5.0.0 shipped
on 2026-05-02", "Shipped in v5.0.0") are explicitly NOT covered
by this guard — those are accurate historical fact.

What this test guards:

- the literal version string in `pyproject.toml` matches
  `mnemos/_version.py` (already pinned by the release script
  but worth a unit-test backstop)
- "current" version claims in docs match `__version__`
- pip install pins (`mnemos-os==X`) in operator-facing docs match
- single-binary release URL (`releases/download/vX/...`) matches

The checks above pin ten named phrases in ten named files, which is a
floor rather than a ceiling: a doc that invents its own wording, or one
nobody remembered to add to the list, drifts unseen. That happened again
in the 6.1 audit, so two repo-wide guards were added at the bottom of
this module:

- `test_no_active_doc_claims_a_superseded_version_is_current` scans every
  markdown file outside `docs/history/` for the *shape* of a currency
  claim, whatever wording it uses.
- `test_no_internal_infrastructure_detail_in_docs` keeps internal host
  names, private addresses, and real DSN passwords out of the published
  tree — including the archive, which is just as public.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _current_version() -> str:
    """Read the canonical version from `mnemos/_version.py`."""
    src = (REPO / "mnemos" / "_version.py").read_text()
    m = re.search(r'__version__\s*=\s*"([^"]+)"', src)
    assert m, "could not parse __version__ from mnemos/_version.py"
    return m.group(1)


def test_pyproject_version_matches_version_py():
    """pyproject.toml and `mnemos/_version.py` must agree on the
    package version. This is also enforced by the release script;
    we add a unit-test backstop so a bad commit can't sneak past
    a missing release dry-run."""
    pyproject = (REPO / "pyproject.toml").read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert m, "could not parse version from pyproject.toml"
    assert m.group(1) == _current_version(), (
        f"pyproject.toml version {m.group(1)} does not match "
        f"mnemos/_version.py {_current_version()} — bump both."
    )


@pytest.mark.parametrize("relpath,phrase_template", [
    # operator-facing front matter
    ("README.md", "MNEMOS v{V} is"),
    ("DEPLOYMENT.md", "v{V} current"),
    ("API_DOCUMENTATION.md", "v{V} current"),
    ("SYSTEM_REQUIREMENTS.md", "current v{V} release line"),
    ("ROADMAP.md", "Current status — v{V}"),
    ("EVOLUTION.md", "current v{V} release line"),
    ("docs/OPERATIONS.md", "v{V} production line"),
    ("QUICK_START_REQUIREMENTS.md", "current v{V} release line"),
    ("docs/GRAEAE_FEATURES.md", "current v{V} documentation"),
    ("docs/SPECIFICATION.md", "v{V} current"),
])
def test_current_version_claim_in_doc(relpath: str, phrase_template: str):
    """Each operator-facing doc names its current-version claim.
    The phrase must appear with the live package version."""
    version = _current_version()
    expected = phrase_template.format(V=version)
    src = (REPO / relpath).read_text()
    assert expected in src, (
        f"{relpath} lacks the current-version phrase `{expected}`. "
        "If the wording was intentionally rephrased, also update "
        "this test to match — but don't let it drift back to a "
        "stale version string."
    )


def test_no_stale_install_pins_to_old_version():
    """Pip install pins in operator docs must use the live version
    or remain in MIGRATION sections clearly labelled as historical.
    Pin: no `==<old>` install commands outside CHANGELOG.md or
    explicitly historical sections.
    """
    version = _current_version()
    # CHANGELOG.md, EVOLUTION.md, and ROADMAP.md are
    # historical-narrative surfaces; they're not in the iterated
    # `for md in [...]` list below, which is the actual filter.
    # Match any pip install pin to a 4.x or 5.0.x version.
    pattern = re.compile(
        r"pip install\s+'?mnemos-os(?:\[[^\]]+\])?==(?P<v>[0-9][0-9.a-zA-Z]*)'?"
    )
    bad: list[str] = []
    for md in [REPO / "README.md", REPO / "DEPLOYMENT.md",
               REPO / "QUICK_START_REQUIREMENTS.md",
               REPO / "docs" / "INSTALL.md",
               REPO / "API_DOCUMENTATION.md",
               REPO / "docs" / "OPERATIONS.md",
               REPO / "docs" / "GRAEAE_FEATURES.md"]:
        if not md.exists():
            continue
        for lineno, line in enumerate(md.read_text().splitlines(),
                                      start=1):
            m = pattern.search(line)
            if not m:
                continue
            pinned = m.group("v")
            if pinned == version:
                continue
            # Section "Migration From Earlier v5 Installs" in
            # docs/INSTALL.md historical narrative around line
            # 85 mentions ==5.0.0 to describe past behavior.
            if md.name == "INSTALL.md" and "behaved like an" in line:
                continue
            bad.append(
                f"  {md.relative_to(REPO)}:{lineno}: pinned to "
                f"{pinned}, expected {version} — "
                f"{line.strip()[:80]}"
            )
    assert not bad, (
        f"{len(bad)} stale install pin(s) outside CHANGELOG / "
        f"EVOLUTION / ROADMAP:\n" + "\n".join(bad)
    )


def test_no_stale_health_json_version():
    """Doc-embedded `/health` JSON examples must use the live
    version. Codex round-1 of #193 caught two of these
    (API_DOCUMENTATION.md + docs/OPERATIONS.md). Pin so future
    drift trips at test time."""
    version = _current_version()
    pattern = re.compile(r'"version":\s*"(?P<v>\d[^"]*)"')
    bad: list[str] = []
    for md in [REPO / "API_DOCUMENTATION.md",
               REPO / "docs" / "OPERATIONS.md"]:
        if not md.exists():
            continue
        for lineno, line in enumerate(md.read_text().splitlines(),
                                      start=1):
            m = pattern.search(line)
            if not m:
                continue
            v = m.group("v")
            if v == version:
                continue
            bad.append(
                f"  {md.relative_to(REPO)}:{lineno}: stale "
                f"{v!r} in /health JSON example, expected "
                f"{version!r}"
            )
    assert not bad, (
        f"{len(bad)} stale /health JSON version example(s):\n"
        + "\n".join(bad)
    )


def test_no_stale_docker_image_tag():
    """Docker `ghcr.io/mnemos-os/mnemos:<tag>` references in
    operator/connector docs must pin to the live version. Codex
    round-1 of #193 caught a `:4.0.0` pin in
    `docs/connectors/chatgpt-pro-developer-mode.md`."""
    version = _current_version()
    pattern = re.compile(
        r"ghcr\.io/mnemos-os/mnemos:(?P<v>[0-9.]+)"
    )
    bad: list[str] = []
    operator_docs: list[Path] = [REPO / "README.md",
                                 REPO / "DEPLOYMENT.md"]
    if (REPO / "docs" / "connectors").exists():
        operator_docs.extend(
            (REPO / "docs" / "connectors").glob("*.md")
        )
    for md in operator_docs:
        if not md.exists():
            continue
        for lineno, line in enumerate(md.read_text().splitlines(),
                                      start=1):
            m = pattern.search(line)
            if not m:
                continue
            v = m.group("v")
            if v == version:
                continue
            bad.append(
                f"  {md.relative_to(REPO)}:{lineno}: stale "
                f"image tag :{v}, expected :{version}"
            )
    assert not bad, (
        f"{len(bad)} stale Docker image tag(s):\n"
        + "\n".join(bad)
    )


def test_no_stale_tracks_mnemos_server_marker():
    """Doc-footer "Tracks MNEMOS server vX" markers in
    architecture / observability docs must pin to the live
    version. Codex round-4 of #193 caught two of these."""
    version = _current_version()
    pattern = re.compile(
        r"Tracks MNEMOS server v(?P<v>\d+(?:\.\d+)*(?:[a-zA-Z]+\d+)?)"
    )
    bad: list[str] = []
    docs_dir = REPO / "docs"
    if docs_dir.exists():
        for md in docs_dir.rglob("*.md"):
            for lineno, line in enumerate(md.read_text().splitlines(),
                                          start=1):
                m = pattern.search(line)
                if not m:
                    continue
                v = m.group("v")
                if v == version:
                    continue
                bad.append(
                    f"  {md.relative_to(REPO)}:{lineno}: stale "
                    f"`Tracks MNEMOS server v{v}` (expected v{version})"
                )
    assert not bad, (
        f"{len(bad)} stale `Tracks MNEMOS server vX` marker(s):\n"
        + "\n".join(bad)
    )


def test_no_stale_as_of_version_anywhere():
    """`As of vX` / `as of vX` markers anywhere under docs/**/*.md
    or docs/**/*.json (Grafana dashboards) must pin to the live
    version. Round-5 broadened scope from runbooks-only to all
    docs after codex round-5 caught more drift in OBSERVABILITY,
    MEMORY_ARCHITECTURE, and the Grafana JSON.

    Case-insensitive `as of` so both `As of v5.3.2` and `as of
    v5.3.2` match.
    """
    version = _current_version()
    pattern = re.compile(
        r"\b[Aa]s of v(?P<v>\d+(?:\.\d+)*(?:[a-zA-Z]+\d+)?)"
    )
    bad: list[str] = []
    docs_dir = REPO / "docs"
    surfaces: list[Path] = []
    if docs_dir.exists():
        surfaces.extend(docs_dir.rglob("*.md"))
        surfaces.extend(docs_dir.rglob("*.json"))
    for md in surfaces:
        for lineno, line in enumerate(md.read_text().splitlines(),
                                      start=1):
            m = pattern.search(line)
            if not m:
                continue
            v = m.group("v")
            if v == version:
                continue
            bad.append(
                f"  {md.relative_to(REPO)}:{lineno}: stale "
                f"`as of v{v}` (expected v{version})"
            )
    assert not bad, (
        f"{len(bad)} stale `as of vX` marker(s):\n"
        + "\n".join(bad)
    )


def test_no_stale_release_download_url():
    """Single-binary download URLs must reference the live
    version. Historical URLs in CHANGELOG / EVOLUTION are fine.
    """
    version = _current_version()
    pattern = re.compile(
        r"releases/download/v(?P<v>[0-9.]+)/mnemos-linux-x86_64"
    )
    bad: list[str] = []
    for md in [REPO / "README.md", REPO / "DEPLOYMENT.md",
               REPO / "QUICK_START_REQUIREMENTS.md"]:
        if not md.exists():
            continue
        for lineno, line in enumerate(md.read_text().splitlines(),
                                      start=1):
            m = pattern.search(line)
            if not m:
                continue
            url_v = m.group("v")
            if url_v == version:
                continue
            bad.append(
                f"  {md.relative_to(REPO)}:{lineno}: stale "
                f"v{url_v} URL, expected v{version}"
            )
    assert not bad, (
        f"{len(bad)} stale single-binary download URL(s):\n"
        + "\n".join(bad)
    )


# ---------------------------------------------------------------------------
# Repo-wide guards.
#
# The parametrised checks above pin ten named phrases in ten named files.
# That is a floor, not a ceiling: it cannot see a doc that invents its own
# wording, and it cannot see a doc nobody thought to add to the list. Docs
# have drifted back to a stale "current version" at least three times, and
# each time the fix was by hand.
#
# The two checks below scan every markdown file instead of a fixed list.
# ---------------------------------------------------------------------------

# Directories whose contents are deliberately historical or not ours.
_SKIP_DIRS = frozenset({
    ".git", "build", "node_modules", ".venv", ".venv-ci", "dist",
    # docs/history/ is an explicitly labelled archive: it is *supposed* to
    # describe superseded releases. See docs/history/README.md.
    "history",
})

# CHANGELOG is a release-by-release record; every entry names its own version.
_SKIP_FILES = frozenset({"CHANGELOG.md"})

# A version-shaped token: 6.1, 6.1.7, 4.2.0a14.
_VER = r"v?(\d+\.\d+(?:\.\d+)?(?:[a-zA-Z]+\d+)?)"
# Same, but the leading "v" is required. Used where a bare number would
# otherwise match a numbered markdown heading such as "### 7.1 Current state".
_VVER = r"v(\d+\.\d+(?:\.\d+)?(?:[a-zA-Z]+\d+)?)"

# Phrasings that assert a version is the *live* one. Historical statements
# ("shipped in v5.0.0", "as of v3.2.4", "v2.4 ships in v3") deliberately do
# not match: they are accurate fact and must stay readable.
_CURRENCY_CLAIMS = tuple(re.compile(p, re.IGNORECASE) for p in (
    rf"current(?:ly)?\s+(?:is\s+)?v{_VER[1:]}\b",
    rf"current(?:ly)?\s+(?:is\s+)?(?:version\s+){_VER}\b",
    rf"\b{_VVER}\s+current\b",
    rf"\bcurrent\s+{_VER}\s+release\b",
    rf"\b{_VVER}\s+(?:is|are)\s+(?:the\s+)?current\b",
    rf"\bthe\s+current\s+{_VER}\b",
    rf"^\s*\**Status\**\s*[:\-]\s*{_VER}\b",
    rf"\bfull\s+{_VER}\s+feature\s+set\b",
    rf"\bTracks\s+MNEMOS\s+server\s+{_VER}\b",
))

# Version-shaped strings that are not the MNEMOS version. Matching one of
# these on the same line suppresses the finding.
_NOT_OUR_VERSION = re.compile(
    r"Apache License|Developer Certificate|python-oracledb|\bDb2\b|\bMySQL\b"
    r"|\bMariaDB\b|\bOracle\b|\bPostgreSQL\b|\bPython\b|\bNode\b"
    r"|Document version|schema_version|mif|MIF|\bJSON\b",
)


def _markdown_files() -> list[Path]:
    out: list[Path] = []
    for md in sorted(REPO.rglob("*.md")):
        if any(part in _SKIP_DIRS for part in md.parts):
            continue
        if md.name in _SKIP_FILES:
            continue
        out.append(md)
    return out


def test_no_active_doc_claims_a_superseded_version_is_current():
    """No live doc may assert that a non-current version is current.

    This is the guard the ten pinned phrases above could not provide: it
    reads every markdown file outside the archive and looks for the *shape*
    of a currency claim, whatever wording the author chose.

    If this fails on a legitimate non-MNEMOS version, add its marker to
    `_NOT_OUR_VERSION` rather than deleting the assertion.
    """
    version = _current_version()
    minor = ".".join(version.split(".")[:2])
    bad: list[str] = []
    for md in _markdown_files():
        for lineno, line in enumerate(md.read_text().splitlines(), start=1):
            if _NOT_OUR_VERSION.search(line):
                continue
            for rx in _CURRENCY_CLAIMS:
                m = rx.search(line)
                if not m:
                    continue
                claimed = m.group(1)
                if claimed in (version, minor):
                    continue
                bad.append(
                    f"  {md.relative_to(REPO)}:{lineno}: claims v{claimed} "
                    f"is current (live version is {version}) — "
                    f"{line.strip()[:90]}"
                )
                break
    assert not bad, (
        f"{len(bad)} doc(s) assert a superseded version is current:\n"
        + "\n".join(bad)
    )


# Internal infrastructure that must never appear in a published doc. The
# repo is public; these were removed wholesale in the 6.1 documentation
# audit and this keeps them out.
_INTERNAL_HOSTS = re.compile(
    r"\b(PYTHIA|CERBERUS|ACHILLES|PEGASUS|TYDEUS|TYPHON|ARGONAS|MEDUSA"
    r"|PROTEUS|CYCLOPS|cixmini|clawpi|bigpi)\b"
)
_PRIVATE_ADDR = re.compile(r"\b(?:192\.168|10\.110)\.\d{1,3}\.\d{1,3}\b")
_DSN_WITH_PASSWORD = re.compile(
    r"\b(?:postgres|postgresql|oracle|db2|mysql|mariadb)://"
    r"[^\s:@/]+:(?!<|\$|\{)(?P<pw>[^\s:@/]+)@"
)

# Passwords that are self-evidently not real. A doc example needs *a*
# password-shaped token; these are the ones that say "fill me in".
_PLACEHOLDER_PW = frozenset({
    "pass", "PASS", "password", "PASSWORD", "passwd", "secret", "SECRET",
    "yourpassword", "your_password", "changeme", "CHANGEME", "xxx", "XXX",
    "mypassword", "hunter2", "REDACTED", "placeholder",
})


def test_no_internal_infrastructure_detail_in_docs():
    """Published docs must not name internal hosts, private addresses, or
    embed a DSN with a real password.

    Unlike the currency guard, this one covers docs/history/ too: the
    archive is just as public as the rest of the tree.

    Placeholder DSNs are fine — `<password>`, `$PGPASSWORD`, and `${VAR}`
    forms are all excluded.
    """
    bad: list[str] = []
    for md in sorted(REPO.rglob("*.md")):
        if any(p in {".git", "build", "node_modules", ".venv", ".venv-ci", "dist"}
               for p in md.parts):
            continue
        for lineno, line in enumerate(md.read_text().splitlines(), start=1):
            for label, rx in (
                ("internal hostname", _INTERNAL_HOSTS),
                ("private address", _PRIVATE_ADDR),
                ("DSN with password", _DSN_WITH_PASSWORD),
            ):
                m = rx.search(line)
                if m and rx is _DSN_WITH_PASSWORD:
                    if m.group("pw") in _PLACEHOLDER_PW:
                        continue
                if m:
                    bad.append(
                        f"  {md.relative_to(REPO)}:{lineno}: {label} "
                        f"{m.group(0)!r} — {line.strip()[:80]}"
                    )
                    break
    assert not bad, (
        f"{len(bad)} internal-detail leak(s) in published docs:\n"
        + "\n".join(bad)
    )
