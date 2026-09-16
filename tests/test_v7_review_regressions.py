"""Adversarial regression coverage for the v7 extraction and static inventory."""

import subprocess
import sys

import pytest

from scripts import generate_backend_parity_matrix as parity


@pytest.mark.parametrize("first", ["oracle_audit", "oracle", "db2"])
def test_oracle_modules_import_in_either_order(first):
    subprocess.run(
        [
            sys.executable,
            "-c",
            f"import mnemos.persistence.{first}; "
            "from mnemos.persistence.oracle import OracleBackend, OracleAuditChainRepository; "
            "from mnemos.persistence.oracle_audit import OracleAuditJournalMixin; "
            "assert OracleBackend.create_journal_entry is OracleAuditJournalMixin.create_journal_entry; "
            "assert not OracleAuditChainRepository.__abstractmethods__",
        ],
        check=True,
    )


def test_parity_same_file_c3_override_and_none_stub(tmp_path):
    path = tmp_path / "backend.py"
    path.write_text("""class Root:
    @property
    def memories(self): return self.repo
class Left(Root): pass
class Right(Root):
    @property
    def memories(self): return None
class Backend(Left, Right): pass
""")
    view = parity._view_for_class(path, "Backend")
    assert not parity._property_implemented(view, ("memories",))


def test_parity_resolves_import_alias(tmp_path, monkeypatch):
    (tmp_path / "parent.py").write_text("class Parent:\n @property\n def memories(self): return self.repo\n")
    path = tmp_path / "backend.py"
    path.write_text("from parent import Parent as Renamed\nclass Backend(Renamed): pass\n")
    monkeypatch.setattr(parity, "REPO_ROOT", tmp_path)
    assert parity._property_implemented(parity._view_for_class(path, "Backend"), ("memories",))


def test_parity_does_not_claim_execution():
    rendered = parity._render_markdown([])
    assert "not behavioral parity or passing-test evidence" in rendered
    assert "fully covered" not in rendered


@pytest.mark.asyncio
async def test_benchmark_dispatches_overlapping_tasks_and_checks_count():
    from scripts.bench_sqlite_throughput_phase1 import _run_one

    result = await _run_one(12, 3, embedding_dim=8, warmup=20, readback_count=1)
    assert result["peak_in_flight_inserts"] == 3
    assert result["warmup_inserts"] == 12
    assert result["readback_total_memories"] == 24
    assert result["latency"]["n"] == 12


@pytest.mark.parametrize("change", ["constant", "import", "default", "decorator"])
def test_method_guard_rejects_changed_bindings_and_syntax(tmp_path, monkeypatch, change):
    from tools import verify_method_move as move

    before = "from math import floor\nSCALE = 2\ndef f(x=SCALE):\n return floor(x) * SCALE\n"
    after = before
    if change == "constant":
        after = after.replace("SCALE = 2", "SCALE = 3")
    if change == "import":
        after = after.replace("from math import floor", "from math import ceil as floor")
    if change == "default":
        after = after.replace("x=SCALE", "x=3")
    if change == "decorator":
        after = after.replace("def f", "@staticmethod\ndef f")
    (tmp_path / "new.py").write_text(after)
    (tmp_path / "old.py").write_text("")
    monkeypatch.setattr(move, "REPO_ROOT", tmp_path)

    def read(ref, path):
        if path == "old.py":
            return before
        raise subprocess.CalledProcessError(1, ["git"])

    monkeypatch.setattr(move, "_read_git_file", read)
    result = move.check_symbol(
        symbol="f", before_ref="base", before_file="old.py", after_file="new.py", before_class=None, after_class=None
    )
    assert not result.passed


@pytest.mark.parametrize(
    ("body", "supported"),
    [
        ("pass", False),
        ("return None", False),
        ('"Unlike a stub this never raises BackendCapabilityMissing"; return self.repo', True),
        ("raise NotImplementedError()", False),
    ],
)
def test_parity_accessor_body_not_docstrings(tmp_path, body, supported):
    path = tmp_path / "backend.py"
    path.write_text("class Backend:\n @property\n def memories(self):\n  " + body + "\n")
    assert parity._property_implemented(parity._view_for_class(path, "Backend"), ("memories",)) is supported


@pytest.mark.asyncio
async def test_benchmark_cancels_workers_and_removes_db_on_insert_failure(monkeypatch, tmp_path):
    from scripts import bench_sqlite_throughput_phase1 as bench
    from mnemos.persistence.sqlite import SqliteMemoryRepository

    db_dir = tmp_path / "database"
    db_dir.mkdir()
    monkeypatch.setattr(bench.tempfile, "mkdtemp", lambda **kw: str(db_dir))

    async def fail(*args, **kwargs):
        raise RuntimeError("injected insert failure")

    monkeypatch.setattr(SqliteMemoryRepository, "insert_memory", fail)
    with pytest.raises(RuntimeError, match="injected insert failure"):
        await bench._run_one(12, 3, embedding_dim=8, warmup=0, readback_count=1)
    assert not db_dir.exists()
