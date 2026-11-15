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


# ---------------------------------------------------------------------------
# Backlog section 6 (2026-10-17): the webhooks row was keyed on the runtime
# ``supports_webhooks`` flag, so every backend that implements
# WebhookRepository *without* a dedicated flag (sqlite, mysql, mariadb,
# oracle, db2) read "test candidate, no surface". The fix re-keys the row
# on the facade's ``.webhooks`` accessor. These negative tests pin the
# detector so the regression cannot silently return: a facade with the
# property must be detected; a facade whose property returns None / raises
# (the stub contract) must NOT be detected; a facade with no property at
# all must NOT be detected.
# ---------------------------------------------------------------------------


def test_webhook_detector_detects_facade_with_real_repo(tmp_path):
    """A facade exposing a working ``.webhooks`` repo counts as surface —
    even when the backend has NO ``supports_webhooks`` class attribute
    (the facade-less direct-ABC shape that the old flag-keyed row missed).
    """
    path = tmp_path / "backend.py"
    path.write_text(
        "class Backend:\n"
        "    supports_webhooks = False\n"
        "    @property\n"
        "    def webhooks(self):\n"
        "        return self._webhooks\n"
    )
    view = parity._view_for_class(path, "Backend")
    assert parity._property_implemented(view, ("webhooks",)) is True


def test_webhook_detector_rejects_none_stub(tmp_path):
    """A ``.webhooks`` property that returns None is the documented stub
    contract (no repository wired) — it must NOT count as surface.
    """
    path = tmp_path / "backend.py"
    path.write_text(
        "class Backend:\n"
        "    @property\n"
        "    def webhooks(self):\n"
        "        return None\n"
    )
    view = parity._view_for_class(path, "Backend")
    assert parity._property_implemented(view, ("webhooks",)) is False


def test_webhook_detector_rejects_raising_stub(tmp_path):
    """A ``.webhooks`` property that unconditionally raises
    NotImplementedError / BackendCapabilityMissing is a stub — must NOT
    count as surface.
    """
    path = tmp_path / "backend.py"
    path.write_text(
        "class Backend:\n"
        "    @property\n"
        "    def webhooks(self):\n"
        "        raise NotImplementedError('no webhooks here')\n"
    )
    view = parity._view_for_class(path, "Backend")
    assert parity._property_implemented(view, ("webhooks",)) is False


def test_webhook_detector_rejects_absent_property(tmp_path):
    """A facade that defines no ``.webhooks`` property at all must NOT
    count as surface — the detector must not invent coverage.
    """
    path = tmp_path / "backend.py"
    path.write_text(
        "class Backend:\n"
        "    supports_webhooks = False\n"
        "    @property\n"
        "    def memories(self):\n"
        "        return self._memories\n"
    )
    view = parity._view_for_class(path, "Backend")
    assert parity._property_implemented(view, ("webhooks",)) is False


def test_webhooks_row_is_facade_keyed_not_flag_keyed():
    """Regression pin for the backlog-section-6 false-negative: the
    webhooks capability must be keyed on the facade ``webhooks`` accessor,
    not on the ``supports_webhooks`` flag. If this drifts back to
    ``flag`` / ``supports_webhooks`` the four facade-backed repositories
    silently disappear from the matrix again.
    """
    webhooks = next(c for c in parity._capabilities() if c.id == "webhooks")
    assert webhooks.impl_kind == "webhook_repo"
    assert "webhooks" in webhooks.impl_property
    assert "supports_webhooks" not in webhooks.impl_property


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


# ---------------------------------------------------------------------------
# NG-MN-14 (2026-11): rule D must couple both signals in the SAME test
# function.  A zero-arg global invariant (e.g.
# ``test_acl_default_cache_is_disabled()``) sharing a file with an
# unrelated ``test_postgres_*`` helper used to flip backend cells to
# "surface + test candidate" with no backend coverage — the postgres
# acl/ledger false greens.  These pin the coupling both ways: zero-arg
# fails, fixture-taking passes.
# ---------------------------------------------------------------------------


def test_parity_rule_d_requires_fixture_on_capability_fn(tmp_path):
    """Zero-arg capability fn + unrelated backend-prefixed fn => no match."""
    path = tmp_path / "test_mixed.py"
    path.write_text(
        "def test_acl_default_cache_is_disabled():\n"
        "    pass\n"
        "\n"
        "\n"
        "def test_postgres_upgrade_psql_command_is_well_formed():\n"
        "    pass\n"
    )
    assert parity._test_file_matches(path, "postgres", "acl") is False


def test_parity_rule_d_accepts_fixture_taking_capability_fn(tmp_path):
    """Capability fn with a fixture + backend-prefixed fn => match."""
    path = tmp_path / "test_mixed.py"
    path.write_text(
        "def test_acl_grant_roundtrip(backend_case):\n"
        "    pass\n"
        "\n"
        "\n"
        "def test_postgres_transactional_uses_pool():\n"
        "    pass\n"
    )
    assert parity._capability_referencing_fn_takes_fixture(
        __import__("ast").parse(path.read_text()), "acl", backend="postgres"
    ) is True
