"""Smoke test for the v7 Feature 5/5 Phase 1 SQLite throughput bench.

This is the structural backstop for the deliverable: the bench script
itself, the canonical artifact pair, and the markdown docs page that
references it. The test does NOT run the actual 6-cell matrix — that
runs separately via ``python scripts/bench_sqlite_throughput_phase1.py
--dry-run`` so the loop's automated gate does not pollute ``docs/proof/``
on every CI pass. What this test pins:

- the bench script exists, is importable as a module, and exposes a
  ``main()`` entry point that runs cleanly under ``python -m``
- the argparse defaults match the Phase 1 spec (corpus sizes
  ``{1000, 10000}``, concurrency ``{1, 5, 10}``) — the values the
  roadmap shipping criterion calls out
- the ``--dry-run`` flag exists, defaults to False, and writes to a
  tempdir (NOT ``docs/proof/``) so the gate cannot pollute the
  published-artifact tree
- the committed canonical artifact pair under ``docs/proof/`` is a
  valid JSON document with exactly the 6 expected
  (corpus_size, concurrency) cells, each carrying provenance tags
  (git SHA, UTC timestamp, Python version, platform)
- the markdown summary in ``docs/benchmarks/sqlite-throughput-phase1``
  points at the committed artifact and is wired into the
  benchmarks README so a reader landing on either side discovers
  the other

If any of the above drift, this test trips at test time rather than
during a reviewer re-run of the bench.

v7 Feature 5/5 is intentionally Phase 1 only — the ``--corpus-sizes``
and ``--concurrency`` flags on the bench script accept larger values,
so Phase 2 is a matrix widening, not a script rewrite. This test does
not assert the absence of Phase 2 logic because the design choice is
that the script's CLI is the natural extension point.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "bench_sqlite_throughput_phase1.py"
DOCS_PAGE = REPO / "docs" / "benchmarks" / "sqlite-throughput-phase1-2026-09-15.md"
COMPRESSION_README = REPO / "benchmarks" / "README.md"


# ── helpers ──────────────────────────────────────────────────────────────────


def _load_bench_module():
    """Import the bench script as a module without executing main()."""
    spec = importlib.util.spec_from_file_location(
        "bench_sqlite_throughput_phase1", SCRIPT
    )
    if spec is None or spec.loader is None:
        pytest.fail(f"could not load module spec for {SCRIPT}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _canonical_artifact_paths() -> list[Path]:
    """Return the canonical (committed) artifact pair(s) under docs/proof/.

    The Phase 1 deliverable is a single canonical pair — one JSON +
    one MD sharing the same timestamp stem. This helper returns both
    files when exactly one pair exists, raises on zero or multiple
    pairs so accidental multi-pair drift is caught at test time
    rather than during a reviewer re-run.
    """
    proof = REPO / "docs" / "proof"
    jsons = sorted(proof.glob("bench-sqlite-phase1-*.json"))
    if len(jsons) != 1:
        pytest.fail(
            f"Expected exactly one canonical Phase 1 artifact JSON in "
            f"docs/proof/, found {len(jsons)}: {[j.name for j in jsons]}. "
            f"A multi-pair tree usually means the check command wrote a "
            f"fresh artifact pair on every run — use --dry-run for the "
            f"validation gate instead."
        )
    json_path = jsons[0]
    stem = json_path.name[: -len(".json")]
    md_path = proof / f"{stem}.md"
    if not md_path.exists():
        pytest.fail(
            f"Canonical Phase 1 JSON artifact {json_path.name} has no "
            f"matching markdown summary at {md_path.relative_to(REPO)}"
        )
    return [json_path, md_path]


# ── script shape ─────────────────────────────────────────────────────────────


def test_bench_script_exists_and_is_valid_python():
    """The bench script must exist at the documented path and parse
    cleanly as Python."""
    assert SCRIPT.exists(), f"missing bench script at {SCRIPT}"
    # Byte-compile to surface SyntaxError before any actual execution.
    import py_compile
    py_compile.compile(str(SCRIPT), doraise=True)


def test_bench_module_imports_and_exposes_main():
    """Importing the bench script (without running main) must succeed
    and the module must expose a callable ``main()``."""
    mod = _load_bench_module()
    assert callable(getattr(mod, "main", None)), (
        "scripts/bench_sqlite_throughput_phase1.py must expose a "
        "callable `main()` entry point"
    )


def test_bench_module_docstring_says_phase1_only():
    """The module docstring must declare Phase 1 scope so a reader of
    the source knows Phase 2 is intentionally NOT implemented here."""
    src = SCRIPT.read_text(encoding="utf-8")
    # Match the docstring header — case-insensitive, partial.
    assert "Phase 1" in src, (
        "scripts/bench_sqlite_throughput_phase1.py must declare "
        "Phase 1 scope somewhere prominent (module docstring or "
        "top-of-file comment)"
    )
    assert "Phase 2" in src, (
        "scripts/bench_sqlite_throughput_phase1.py must mention "
        "Phase 2 explicitly so a reader knows it is intentionally "
        "NOT implemented in this script"
    )


# ── CLI shape ────────────────────────────────────────────────────────────────


def _parsed_defaults() -> dict[str, object]:
    """Invoke --help and extract the documented defaults for the
    Phase-1 CLI flags. We avoid running the actual bench loop here —
    that happens via the loop's automated gate which uses --dry-run.

    argparse --help renders the default value into the help string in
    one of two forms:

    - ``(default: <repr>)`` on the flag's own line (older style),
    - ``default: <repr>.`` in the help text on subsequent lines
      (the form this bench uses, because each help string spans
      multiple lines).

    We accept both forms: capture ``default: VALUE`` either inside a
    parens group on the same line, or as a phrase anywhere in the
    help text after the flag name and before the next flag.
    """
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, (
        f"`python {SCRIPT.name} --help` failed: "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    help_text = proc.stdout
    # argparse --help puts each flag definition into an "options:"
    # block AFTER the usage line. The usage line itself also names
    # the flags, so we must skip past `options:` (or `\noptions:\n`)
    # before searching for flag sections, otherwise the usage block's
    # wrapped continuation confuses the search.
    options_marker = help_text.find("\noptions:")
    if options_marker < 0:
        options_marker = help_text.find("options:")
    if options_marker < 0:
        # No "options:" marker — argparse rendered it bare (Python
        # 3.10+ does this). Fall back to the whole help text.
        options_block = help_text
    else:
        options_block = help_text[options_marker:]
    # Split into "sections" per flag: each section starts at the
    # `--flag NAME` line and runs until the next `--` flag line.
    # Then search that section for `default: VALUE`.
    flag_order = [
        "--corpus-sizes",
        "--concurrency",
        "--embedding-dim",
        "--warmup",
        "--readback-count",
        "--output-dir",
        "--dry-run",
    ]
    out: dict[str, object] = {}
    for idx, flag in enumerate(flag_order):
        start = options_block.find("\n" + flag + " ")
        if start < 0:
            start = options_block.find(flag + " ")
            if start < 0:
                out[flag] = None
                continue
            else:
                start = start + 1  # we want to start AT the flag line
        else:
            start = start + 1  # skip the leading \n we matched
        # End of this section: start of the next flag (or EOF).
        end_candidates = []
        for later in flag_order[idx + 1:]:
            pos = options_block.find("\n" + later + " ", start + 1)
            if pos < 0:
                pos = options_block.find(later + " ", start + 1)
            if pos >= 0:
                end_candidates.append(pos)
        end = min(end_candidates) if end_candidates else len(options_block)
        section = options_block[start:end]
        # Capture `default: VALUE` either inside parens on the same
        # line OR as a phrase anywhere in the section (this script's
        # style). Some defaults wrap a numeric value across lines —
        # e.g. "(default 768\n   — matches MNEMOS default)." — so we
        # also accept `default VALUE` followed eventually by a `.`.
        m = re.search(
            r"\(default:\s*(?P<paren>[^)]+)\)", section
        )
        if m:
            out[flag] = m.group("paren").strip()
            continue
        # Walk section line-by-line: capture the first numeric /
        # string token after a `default` keyword on a line by
        # itself or in parens.
        joined = re.sub(r"\s+", " ", section)
        m = re.search(
            r"default[: ]\s*(?P<phrase>[^(\n]+?)(?:\)|\.|\n|\s+[—\u2014-])",
            joined,
        )
        if m:
            candidate = m.group("phrase").strip()
            # Reject phrases that are just the rest of the help text
            # (e.g. "768" is good, "768 matches MNEMOS default" is bad).
            # Take the first token only.
            first = candidate.split()[0] if candidate.split() else candidate
            out[flag] = first.rstrip(",").rstrip(".").strip()
            continue
        # store_true flag: argparse prints no default at all.
        out[flag] = None
    return out


def test_argparse_defaults_match_phase1_spec():
    """Phase 1 spec: corpus sizes {1000, 10000}, concurrency {1, 5, 10},
    embedding_dim 768 (matches the MNEMOS default), output dir
    docs/proof/ (with --dry-run overriding)."""
    defaults = _parsed_defaults()
    assert defaults["--corpus-sizes"] == "1000,10000", (
        f"--corpus-sizes default must be '1000,10000' for Phase 1, "
        f"got {defaults['--corpus-sizes']!r}"
    )
    assert defaults["--concurrency"] == "1,5,10", (
        f"--concurrency default must be '1,5,10' for Phase 1, "
        f"got {defaults['--concurrency']!r}"
    )
    assert defaults["--embedding-dim"] == "768", (
        f"--embedding-dim default must be 768 (matches MNEMOS "
        f"default), got {defaults['--embedding-dim']!r}"
    )
    assert defaults["--output-dir"] == "docs/proof", (
        f"--output-dir default must be 'docs/proof', "
        f"got {defaults['--output-dir']!r}"
    )
    # --dry-run is store_true: argparse prints no default for it,
    # so the key is None in the parsed defaults dict.
    assert defaults["--dry-run"] is None, (
        f"--dry-run must be a store_true flag with no default value, "
        f"got {defaults['--dry-run']!r}"
    )


def test_dry_run_flag_writes_to_tempdir_not_docs_proof():
    """The --dry-run flag must exist (so the loop's automated gate can
    validate the bench without polluting docs/proof/) and must write
    artifacts to a tempfile.mkdtemp directory, NOT docs/proof/.

    We assert the property both structurally (the script handles the
    flag) and behaviorally (a quick --dry-run does not produce a new
    file in docs/proof/).
    """
    # Structural: the source must reference --dry-run.
    src = SCRIPT.read_text(encoding="utf-8")
    assert "--dry-run" in src, (
        "scripts/bench_sqlite_throughput_phase1.py must define a "
        "--dry-run flag so the loop's automated gate can validate "
        "the bench without polluting docs/proof/"
    )

    # Behavioral: snapshot the current docs/proof/ set, run a tiny
    # --dry-run with a single tiny cell, and confirm docs/proof/
    # contents are byte-identical afterwards.
    proof = REPO / "docs" / "proof"
    before = {p.name: p.stat().st_mtime_ns for p in proof.iterdir() if p.is_file()}
    # Use a 1-row corpus at concurrency 1 so this stays cheap.
    proc = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--corpus-sizes", "1",
         "--concurrency", "1",
         "--warmup", "0",
         "--dry-run"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, (
        f"`python {SCRIPT.name} --dry-run` failed: "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    after = {p.name: p.stat().st_mtime_ns for p in proof.iterdir() if p.is_file()}
    assert before == after, (
        "docs/proof/ contents changed after a --dry-run run. "
        f"Before: {sorted(before)}\nAfter:  {sorted(after)}"
    )


# ── canonical artifact pair ─────────────────────────────────────────────────


EXPECTED_CELLS = [
    (1000, 1), (1000, 5), (1000, 10),
    (10000, 1), (10000, 5), (10000, 10),
]


def test_canonical_artifact_pair_exists_and_is_unique():
    """There must be exactly one canonical artifact pair under
    docs/proof/ (one JSON + matching MD). Multi-pair drift usually
    means the check command wrote artifacts on every run; the fix
    is to use --dry-run in the gate."""
    paths = _canonical_artifact_paths()
    assert len(paths) == 2
    # Both files share a timestamp stem (already validated by the
    # helper) — assert the stem matches the docs page reference too.
    json_path = paths[0]
    stem = json_path.name[: -len(".json")]
    assert DOCS_PAGE.exists(), (
        f"docs/benchmarks page missing at {DOCS_PAGE.relative_to(REPO)}"
    )
    assert stem in DOCS_PAGE.read_text(encoding="utf-8"), (
        f"docs/benchmarks page does not reference the canonical "
        f"artifact stem {stem!r}"
    )


def test_canonical_artifact_json_has_six_cells_with_required_keys():
    """The committed JSON must contain exactly the 6 expected
    (corpus_size, concurrency) cells, each with throughput + latency
    distribution + provenance tags."""
    json_path, _ = _canonical_artifact_paths()
    payload = json.loads(json_path.read_text(encoding="utf-8"))

    # Top-level provenance tags the roadmap requires.
    for k in ("schema", "run_utc", "git_sha", "python_version",
              "platform", "backend", "embedding_dim", "cells",
              "total_wall_seconds", "phase"):
        assert k in payload, (
            f"Canonical artifact {json_path.name} missing top-level "
            f"key {k!r}. Has: {sorted(payload.keys())}"
        )
    assert payload["backend"] == "sqlite", (
        f"Canonical artifact backend must be 'sqlite' for Phase 1, "
        f"got {payload['backend']!r}"
    )
    assert payload["phase"] == 1, (
        f"Canonical artifact phase must be 1, got {payload['phase']!r}"
    )
    assert payload["embedding_dim"] == 768, (
        f"Canonical artifact embedding_dim must be 768, "
        f"got {payload['embedding_dim']!r}"
    )

    cells = payload["cells"]
    assert len(cells) == len(EXPECTED_CELLS), (
        f"Canonical artifact must have exactly {len(EXPECTED_CELLS)} "
        f"cells, got {len(cells)}"
    )
    seen = set()
    for cell in cells:
        key = (cell["corpus_size"], cell["concurrency"])
        seen.add(key)
        for k in ("corpus_size", "concurrency", "wall_seconds",
                  "insert_wall_seconds", "throughput_ops_per_sec",
                  "latency", "embedding_dim", "schema_note"):
            assert k in cell, (
                f"Cell {key} missing key {k!r}. Has: {sorted(cell.keys())}"
            )
        lat = cell["latency"]
        for k in ("n", "p50_ms", "p95_ms", "p99_ms", "mean_ms"):
            assert k in lat, (
                f"Cell {key} latency missing key {k!r}. Has: {sorted(lat.keys())}"
            )
        # Throughput must be a positive finite number — the bench is
        # worthless if a cell produced None / 0 silently.
        assert isinstance(cell["throughput_ops_per_sec"], (int, float))
        assert cell["throughput_ops_per_sec"] > 0, (
            f"Cell {key} throughput must be > 0, got "
            f"{cell['throughput_ops_per_sec']!r}"
        )
        # Latency p50 <= p95 <= p99 (sanity for the percentile math).
        assert lat["p50_ms"] <= lat["p95_ms"] <= lat["p99_ms"], (
            f"Cell {key} latency percentiles out of order: "
            f"p50={lat['p50_ms']} p95={lat['p95_ms']} p99={lat['p99_ms']}"
        )
    assert seen == set(EXPECTED_CELLS), (
        f"Canonical artifact cells must cover exactly {EXPECTED_CELLS}, "
        f"got {sorted(seen)}"
    )


def test_canonical_artifact_markdown_renders_all_six_cells():
    """The committed MD must contain a results table that lists all 6
    (corpus_size, concurrency) cells — a missing row means the matrix
    wasn't fully captured in the human-readable summary."""
    _, md_path = _canonical_artifact_paths()
    body = md_path.read_text(encoding="utf-8")
    for corpus_size, concurrency in EXPECTED_CELLS:
        # The markdown table has rows like `| 1000 | 1 | 1652.92 | ...`.
        # Match the first two columns as a stable anchor.
        assert re.search(
            rf"\|\s*{corpus_size}\s*\|\s*{concurrency}\s*\|",
            body,
        ), (
            f"Canonical markdown {md_path.name} missing row for "
            f"(corpus_size={corpus_size}, concurrency={concurrency})"
        )
    # Phase 2 must be explicitly noted as NOT done.
    assert "Phase 2" in body, (
        f"Canonical markdown {md_path.name} must mention Phase 2 as "
        f"intentionally not done in Phase 1"
    )


# ── docs wiring ──────────────────────────────────────────────────────────────


def test_benchmarks_readme_links_to_sqlite_phase1_doc():
    """benchmarks/README.md (the compression README) must point at the
    SQLite Phase 1 docs page + canonical artifact so a reader landing
    on either side discovers the other."""
    assert COMPRESSION_README.exists()
    body = COMPRESSION_README.read_text(encoding="utf-8")
    assert "sqlite-throughput-phase1-2026-09-15.md" in body, (
        "benchmarks/README.md must point at "
        "docs/benchmarks/sqlite-throughput-phase1-2026-09-15.md"
    )
    assert "bench-sqlite-phase1-" in body, (
        "benchmarks/README.md must reference the canonical Phase 1 "
        "artifact stem"
    )


def test_phase1_doc_explains_phase2_blocker():
    """The Phase 1 docs page must explicitly note what Phase 1 measured,
    what it did NOT do, and that Phase 2 is blocked on the operator
    hardware-allocation decision."""
    body = DOCS_PAGE.read_text(encoding="utf-8")
    for phrase in (
        "Phase 1",
        "Phase 2",
        "blocked on an operator hardware-allocation decision",
        "1,000 / 10,000",  # corpus sizes from the scope table
        "1 / 5 / 10",       # concurrency from the scope table
        "sqlite",           # backend
    ):
        assert phrase in body, (
            f"docs/benchmarks/sqlite-throughput-phase1-2026-09-15.md "
            f"missing required phrase {phrase!r}"
        )


def test_phase1_doc_check_command_uses_dry_run():
    """The Phase 1 docs page's "gate results" / check-command section
    must use ``--dry-run`` so the loop's automated gate does not
    pollute ``docs/proof/`` on every CI pass. This is the structural
    fix for the prior review's "multi-pair docs/proof/" failure
    mode."""
    body = DOCS_PAGE.read_text(encoding="utf-8")
    assert "--dry-run" in body, (
        "docs/benchmarks/sqlite-throughput-phase1-2026-09-15.md must "
        "document --dry-run as the check-command path so a CI gate "
        "cannot accidentally append a fresh artifact pair to "
        "docs/proof/ on every run"
    )
