"""Tests for MORPHEUS slice 4: EXTRACT.

Item 11a (ABC migration): ``phase_extract`` still takes a raw
``asyncpg.Pool`` (its own per-row SQL is out of scope), but the
``rollback_run`` helper now routes through
``backend.morpheus.rollback_run``. The two rollback tests at the bottom
of this file therefore need a backend-shaped mock with a
``morpheus.rollback_run`` impl that performs the same SQL semantics
the legacy raw-asyncpg path did — delete kg_triples tagged with the
run, drop the morpheus_extract_run_memories rows, clear
``triples_extracted_at`` on the affected memories, and flip the run row
to ``status='rolled_back'``. The original ``_Pool`` / ``_Conn`` mocks
are retained for the ``phase_extract`` tests (which still take a
pool).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from mnemos.core import config as core_config
from mnemos.domain.morpheus import runner
from mnemos.domain.morpheus.runner import ExtractedTriple, phase_extract, rollback_run


_WINDOW_OPEN = datetime(1970, 1, 1)
_WINDOW_CLOSE = datetime(2999, 1, 1)

RUN_ID = "00000000-0000-0000-0000-0000000000e4"
OTHER_RUN_ID = "00000000-0000-0000-0000-0000000000f5"
_DEFAULT_CONTENT = object()


@pytest.fixture(autouse=True)
def reset_morpheus_extract_settings(monkeypatch):
    monkeypatch.delenv("MNEMOS_MORPHEUS_EXTRACT", raising=False)
    monkeypatch.delenv("MNEMOS_MORPHEUS_EXTRACT_VERIFY", raising=False)
    monkeypatch.delenv("MNEMOS_MORPHEUS_EXTRACT_MIN_CHARS", raising=False)
    monkeypatch.delenv("MNEMOS_MORPHEUS_EXTRACT_MIN_CONFIDENCE", raising=False)
    monkeypatch.delenv("MNEMOS_MORPHEUS_EXTRACT_MUSE", raising=False)
    monkeypatch.delenv("MNEMOS_MORPHEUS_EXTRACT_VERIFIER", raising=False)
    monkeypatch.delenv("MNEMOS_MORPHEUS_EXTRACT_MAX_INPUT_COUNT", raising=False)
    monkeypatch.delenv("MNEMOS_MORPHEUS_EXTRACT_MAX_FAILURES", raising=False)
    core_config._reset_settings_for_tests()
    yield
    core_config._reset_settings_for_tests()


class _Txn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_exc):
        return False


class _Conn:
    def __init__(
        self,
        *,
        run_config: dict | None = None,
        run_namespace: str | None = "A",
        memories: list[dict] | None = None,
        kg_triples: list[dict] | None = None,
        run_window: tuple[datetime, datetime] | None = None,
    ):
        self.run_row = {
            "config": run_config or {"extract": True},
            "namespace": run_namespace,
            "window_started_at": run_window[0] if run_window else _WINDOW_OPEN,
            "window_ended_at": run_window[1] if run_window else _WINDOW_CLOSE,
            "triples_extracted": 0,
            "memories_processed_for_extraction": 0,
        }
        self.memories = {row["id"]: row for row in memories or []}
        self.kg_triples = list(kg_triples or [])
        self.extract_run_memories: list[dict] = []
        self.extract_failures: dict[str, dict] = {}
        self.executed: list[tuple[str, tuple]] = []
        self.counter_updates: list[tuple[str, tuple]] = []

    def transaction(self):
        return _Txn()

    async def fetchrow(self, sql: str, *_args):
        if "FROM morpheus_runs" in sql:
            return self.run_row
        compact = " ".join(sql.split())
        if compact.startswith("INSERT INTO morpheus_extract_failures"):
            memory_id, max_failures, last_error = _args
            attempts = self.extract_failures.get(memory_id, {}).get("attempts", 0) + 1
            status = "dead_letter" if attempts >= max_failures else "retryable"
            row = {
                "memory_id": memory_id,
                "attempts": attempts,
                "status": status,
                "last_error": last_error,
                "last_failed_at": "now",
            }
            self.extract_failures[memory_id] = row
            return row
        return None

    async def fetch(self, sql: str, *args):
        compact = " ".join(sql.split())
        if "SELECT m.id, m.verbatim_content, m.owner_id, m.namespace" not in compact:
            return []
        assert "m.created <= $1" in compact
        assert "ORDER BY m.created, m.id" in compact
        assert "failure.status <> 'dead_letter'" in compact
        assert "LIMIT $4" in compact
        window_end, min_chars, namespace, limit = args
        out = []
        for row in sorted(self.memories.values(), key=lambda item: item["created"]):
            content = row.get("verbatim_content")
            if row.get("deleted_at") is not None:
                continue
            if row.get("archived_at") is not None:
                continue
            if row.get("consolidated_into") is not None:
                continue
            if row.get("namespace") == "vault":
                continue
            if row.get("triples_extracted_at") is not None:
                continue
            if self.extract_failures.get(row["id"], {}).get("status") == "dead_letter":
                continue
            if content is None or len(content) < min_chars:
                continue
            if namespace is not None and row.get("namespace") != namespace:
                continue
            if row["created"] > window_end:
                continue
            out.append(row)
        return out[:limit]

    async def fetchval(self, sql: str, *args):
        compact = " ".join(sql.split())
        if compact.startswith("SELECT id FROM memories") and compact.endswith("FOR UPDATE"):
            row = self.memories.get(args[0])
            if row is not None and row.get("triples_extracted_at") is None:
                return row["id"]
            return None
        if compact.startswith("UPDATE memories SET triples_extracted_at = NOW()"):
            memory_id, namespace = args
            row = self.memories.get(memory_id)
            if row is None or row.get("deleted_at") is not None:
                return None
            if row.get("archived_at") is not None:
                return None
            if row.get("consolidated_into") is not None:
                return None
            if row.get("namespace") == "vault":
                return None
            if row.get("triples_extracted_at") is not None:
                return None
            if namespace is not None and row.get("namespace") != namespace:
                return None
            row["triples_extracted_at"] = "now"
            return memory_id
        return None

    async def execute(self, sql: str, *args):
        self.executed.append((sql, args))
        compact = " ".join(sql.split())
        if compact.startswith("INSERT INTO kg_triples"):
            self.kg_triples.append(
                {
                    "id": args[0],
                    "subject": args[1],
                    "predicate": args[2],
                    "object": args[3],
                    "memory_id": args[4],
                    "confidence": args[5],
                    "extracted_by_run_id": args[6],
                    "owner_id": args[7],
                    "namespace": args[8],
                }
            )
            return "INSERT 0 1"
        if compact.startswith("INSERT INTO morpheus_extract_run_memories"):
            run_id, memory_id = args
            self.extract_run_memories = [
                row
                for row in self.extract_run_memories
                if not (row["run_id"] == run_id and row["memory_id"] == memory_id)
            ]
            self.extract_run_memories.append(
                {
                    "run_id": run_id,
                    "memory_id": memory_id,
                    "processed_at": "now",
                }
            )
            return "INSERT 0 1"
        if compact.startswith("DELETE FROM morpheus_extract_failures"):
            self.extract_failures.pop(args[0], None)
            return "DELETE 1"
        if compact.startswith("WITH deleted_extract_triples AS"):
            return self._execute_extract_rollback(args[0])
        if compact.startswith("UPDATE memories SET consolidated_into = NULL"):
            return "UPDATE 0"
        if compact.startswith("DELETE FROM memories WHERE morpheus_run_id"):
            return "DELETE 0"
        if compact.startswith("UPDATE morpheus_runs"):
            self.counter_updates.append((sql, args))
            if "COALESCE(triples_extracted, 0) +" in compact:
                self.run_row["triples_extracted"] += args[1]
                self.run_row["memories_processed_for_extraction"] += args[2]
            return "UPDATE 1"
        return "OK"

    def _execute_extract_rollback(self, run_id: str) -> str:
        affected_memory_ids = {
            row.get("memory_id")
            for row in self.kg_triples
            if row.get("extracted_by_run_id") == run_id and row.get("memory_id")
        }
        affected_memory_ids.update(row["memory_id"] for row in self.extract_run_memories if row["run_id"] == run_id)
        self.kg_triples = [row for row in self.kg_triples if row.get("extracted_by_run_id") != run_id]
        self.extract_run_memories = [row for row in self.extract_run_memories if row["run_id"] != run_id]
        reset = 0
        for memory_id in affected_memory_ids:
            row = self.memories.get(memory_id)
            if row is not None:
                row["triples_extracted_at"] = None
                reset += 1
        return f"UPDATE {reset}"


class _Pool:
    def __init__(self, conn: _Conn):
        self.conn = conn

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self_inner):
                return pool.conn

            async def __aexit__(self_inner, *_exc):
                return False

        return _Ctx()


class _Morpheus:
    """Stand-in for ``backend.morpheus``.

    Implements ``rollback_run`` to match the SQL semantics the legacy
    raw-asyncpg path had (delete ``kg_triples`` and
    ``morpheus_extract_run_memories`` rows tagged with the run, clear
    ``triples_extracted_at`` on affected memories, flip the run row to
    ``status='rolled_back'``); implements every other lifecycle method
    as a no-op so the EXTRACT phase's internal
    ``update_counters(_get_backend(), ...)`` dispatch doesn't fail when
    the lifecycle global is wired to this backend.
    """

    def __init__(self, conn: _Conn):
        self._conn = conn

    async def begin_run(self, tx, **kwargs):
        return "00000000-0000-0000-0000-000000000000"

    async def set_phase(self, tx, run_id, phase):
        return None

    async def update_counters(self, tx, run_id, **_kwargs):
        return None

    async def increment_extract_counters(
        self, tx, run_id, *, triples_extracted, memories_processed
    ):
        return None

    async def finish_run(self, tx, run_id):
        return None

    async def fail_run(self, tx, run_id, error):
        return None

    async def sweep_orphan_runs(self, tx, *, threshold_hours):
        return []

    async def rollback_run(self, tx, run_id: str, *, requested_by: str):
        # (a) Reset ``triples_extracted_at`` on memories that had KG
        # triples extracted by this run (and on memories listed in
        # ``morpheus_extract_run_memories`` even when no triple landed).
        affected_memory_ids: set[str | None] = {
            row.get("memory_id")
            for row in self._conn.kg_triples
            if row.get("extracted_by_run_id") == run_id and row.get("memory_id")
        }
        affected_memory_ids.update(
            row["memory_id"]
            for row in self._conn.extract_run_memories
            if row["run_id"] == run_id and row.get("memory_id")
        )
        # (b) Delete the kg_triples tagged with this run.
        self._conn.kg_triples = [
            row for row in self._conn.kg_triples
            if row.get("extracted_by_run_id") != run_id
        ]
        # (c) Delete the morpheus_extract_run_memories rows.
        self._conn.extract_run_memories = [
            row for row in self._conn.extract_run_memories
            if row["run_id"] != run_id
        ]
        # (d) Reset triples_extracted_at on the affected memories.
        for memory_id in affected_memory_ids:
            row = self._conn.memories.get(memory_id)
            if row is not None:
                row["triples_extracted_at"] = None
        # (e) EXTRACT phase doesn't create run-owned summary memories
        # (that's CONSOLIDATE/SYNTHESISE) so ``memories_deleted`` is
        # always 0 in this test surface.
        n_deleted = 0
        # (f) Flip the run row to ``rolled_back``. The ``run_rows``
        # mock dict is set up by the rollback test if it wants to
        # verify the post-state; otherwise it's left untouched and we
        # still report 1 row updated.
        if hasattr(self._conn, "run_rows") and self._conn.run_rows is not None:
            for row in self._conn.run_rows.values():
                if row.get("id") == run_id:
                    row["status"] = "rolled_back"
        n_run = 1
        return n_deleted, n_run


class _Backend:
    """Backend-shaped mock — has ``morpheus`` and ``transactional``.

    The runner's ``rollback_run(backend, run_id, *, requested_by)`` opens
    ``backend.transactional()`` and calls
    ``backend.morpheus.rollback_run(tx, run_id, requested_by=...)``. The
    phase functions (``phase_extract`` etc.) call
    ``update_counters(_get_backend(), ...)`` internally on the early-exit
    branches; the no-op ``transactional`` CM and the
    ``_Morpheus.update_counters`` no-op above are sufficient for both
    call sites.
    """

    def __init__(self, conn: _Conn):
        self._conn = conn
        self.morpheus = _Morpheus(conn)

    def transactional(self):

        class _Ctx:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *_exc):
                return False

        return _Ctx()


@pytest.fixture(autouse=True)
def _install_noop_morpheus_backend(monkeypatch):
    """Wire a no-op backend into the lifecycle global for every test.

    Item 11a: ``phase_extract`` internally calls
    ``_get_backend()`` to dispatch ``update_counters`` through the
    new ABC. The lifecycle global ``_persistence_backend`` is None
    by default in this test process, so wire a ``_Backend`` (built
    on an empty ``_Conn``) into the global for the duration of each
    test so the phase function doesn't crash trying to look up a
    backend. The rollback tests use their own ``_Backend(conn)``
    instance with the test-specific state; the autouse backend's
    empty ``_Conn`` is irrelevant for those tests because they
    never exercise ``phase_extract``.
    """
    from mnemos.core import lifecycle as _lifecycle

    backend = _Backend(_Conn())
    monkeypatch.setattr(_lifecycle, "_persistence_backend", backend)


def _long_prose(memory_id: str) -> str:
    return (
        f"{memory_id} captures a durable product decision involving Alice, "
        "Project Helios, and the April launch window. "
        "The prose is intentionally longer than the default extraction "
        "threshold so MORPHEUS treats it as candidate text. "
        "It includes enough detail for multiple simple facts."
    )


def _memory(
    memory_id: str,
    *,
    namespace: str = "A",
    verbatim_content: object = _DEFAULT_CONTENT,
    triples_extracted_at: object | None = None,
    created_offset: int = 0,
    archived_at: object | None = None,
    consolidated_into: str | None = None,
) -> dict:
    content = _long_prose(memory_id) if verbatim_content is _DEFAULT_CONTENT else verbatim_content
    return {
        "id": memory_id,
        "verbatim_content": content,
        "owner_id": f"owner-{namespace}",
        "namespace": namespace,
        "deleted_at": None,
        "archived_at": archived_at,
        "consolidated_into": consolidated_into,
        "triples_extracted_at": triples_extracted_at,
        "created": datetime(2026, 5, 2, 12, 0, 0) + timedelta(minutes=created_offset),
    }


async def _three_triples(content: str) -> list[ExtractedTriple]:
    memory_id = content.split()[0]
    return [
        ExtractedTriple(f"{memory_id}:subject:{idx}", "relates_to", f"{memory_id}:object:{idx}", 0.9)
        for idx in range(3)
    ]


@pytest.mark.asyncio
async def test_phase_extract_three_memories_three_triples_each(monkeypatch):
    monkeypatch.setattr(runner, "_extract_triples_from_prose", _three_triples)
    conn = _Conn(
        memories=[
            _memory("mem_0", created_offset=0),
            _memory("mem_1", created_offset=1),
            _memory("mem_2", created_offset=2),
        ]
    )

    n = await phase_extract(_Pool(conn), RUN_ID)

    assert n == 9
    assert len(conn.kg_triples) == 9
    assert {row["memory_id"] for row in conn.kg_triples} == {"mem_0", "mem_1", "mem_2"}
    assert all(row["extracted_by_run_id"] == RUN_ID for row in conn.kg_triples)
    assert all(row["namespace"] == "A" and row["owner_id"] == "owner-A" for row in conn.kg_triples)
    assert all(row["triples_extracted_at"] == "now" for row in conn.memories.values())


@pytest.mark.asyncio
async def test_phase_extract_idempotent_on_rerun(monkeypatch):
    calls: list[str] = []

    async def fake_extract(content: str) -> list[ExtractedTriple]:
        calls.append(content)
        return await _three_triples(content)

    monkeypatch.setattr(runner, "_extract_triples_from_prose", fake_extract)
    conn = _Conn(
        memories=[
            _memory("mem_0", created_offset=0),
            _memory("mem_1", created_offset=1),
            _memory("mem_2", created_offset=2),
        ]
    )
    pool = _Pool(conn)

    first = await phase_extract(pool, RUN_ID)
    second = await phase_extract(pool, RUN_ID)

    assert first == 9
    assert second == 0
    assert len(calls) == 3
    assert len(conn.kg_triples) == 9


@pytest.mark.asyncio
async def test_phase_extract_skips_null_and_short_verbatim_content(monkeypatch):
    async def one_triple(_content: str) -> list[ExtractedTriple]:
        return [ExtractedTriple("Alice", "owns", "Project Helios", 0.9)]

    monkeypatch.setattr(
        runner,
        "_extract_triples_from_prose",
        one_triple,
    )
    conn = _Conn(
        memories=[
            _memory("null_content", verbatim_content=None, created_offset=0),
            _memory("short_content", verbatim_content="too short", created_offset=1),
            _memory("eligible", created_offset=2),
        ]
    )

    n = await phase_extract(_Pool(conn), RUN_ID)

    assert n == 1
    assert [row["memory_id"] for row in conn.kg_triples] == ["eligible"]
    assert conn.memories["null_content"]["triples_extracted_at"] is None
    assert conn.memories["short_content"]["triples_extracted_at"] is None
    assert conn.memories["eligible"]["triples_extracted_at"] == "now"


@pytest.mark.asyncio
async def test_malformed_muse_json_leaves_memory_retryable(monkeypatch):
    responses = iter(
        [
            "this is not json",
            json.dumps([{"subject": "Alice", "predicate": "owns", "object": "Helios", "confidence": 0.9}]),
        ]
    )

    async def provider_response(*_args, **_kwargs) -> str:
        return next(responses)

    monkeypatch.setattr(runner, "_call_morpheus_muse", provider_response)
    conn = _Conn(memories=[_memory("mem_bad_json")])
    pool = _Pool(conn)

    first = await phase_extract(pool, RUN_ID)

    assert first == 0
    assert conn.kg_triples == []
    assert conn.memories["mem_bad_json"]["triples_extracted_at"] is None
    assert conn.extract_run_memories == []
    assert conn.run_row["memories_processed_for_extraction"] == 0

    second = await phase_extract(pool, RUN_ID)

    assert second == 1
    assert conn.memories["mem_bad_json"]["triples_extracted_at"] == "now"
    assert [row["memory_id"] for row in conn.extract_run_memories] == ["mem_bad_json"]


@pytest.mark.asyncio
async def test_valid_empty_extraction_marks_success(monkeypatch):
    async def valid_empty(*_args, **_kwargs) -> str:
        return "[]"

    monkeypatch.setattr(runner, "_call_morpheus_muse", valid_empty)
    conn = _Conn(memories=[_memory("mem_no_triples")])

    n = await phase_extract(_Pool(conn), RUN_ID)

    assert n == 0
    assert conn.kg_triples == []
    assert conn.memories["mem_no_triples"]["triples_extracted_at"] == "now"
    assert [row["memory_id"] for row in conn.extract_run_memories] == ["mem_no_triples"]
    assert conn.run_row["memories_processed_for_extraction"] == 1


@pytest.mark.asyncio
async def test_provider_failure_leaves_memory_retryable(monkeypatch):
    async def timed_out(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(runner, "_call_morpheus_muse", timed_out)
    conn = _Conn(memories=[_memory("mem_timeout")])

    n = await phase_extract(_Pool(conn), RUN_ID)

    assert n == 0
    assert conn.memories["mem_timeout"]["triples_extracted_at"] is None
    assert conn.extract_run_memories == []
    assert conn.run_row["memories_processed_for_extraction"] == 0


@pytest.mark.asyncio
async def test_late_failure_does_not_recreate_state_after_concurrent_success(monkeypatch):
    conn = _Conn(memories=[_memory("mem_concurrent")])

    async def succeeds_elsewhere_then_fails(_content: str) -> list[ExtractedTriple]:
        conn.memories["mem_concurrent"]["triples_extracted_at"] = "concurrent-success"
        raise runner.MorpheusExtractionError("late provider failure")

    monkeypatch.setattr(runner, "_extract_triples_from_prose", succeeds_elsewhere_then_fails)

    await phase_extract(_Pool(conn), RUN_ID)

    assert conn.extract_failures == {}
    assert conn.memories["mem_concurrent"]["triples_extracted_at"] == "concurrent-success"


@pytest.mark.asyncio
async def test_rollback_run_removes_only_triples_from_that_run():
    conn = _Conn(
        memories=[
            _memory("mem_a", triples_extracted_at="done"),
            _memory("mem_b", triples_extracted_at="done"),
            _memory("mem_c", triples_extracted_at="done"),
        ],
        kg_triples=[
            {"id": "run_a", "memory_id": "mem_a", "extracted_by_run_id": RUN_ID},
            {"id": "run_b", "memory_id": "mem_b", "extracted_by_run_id": RUN_ID},
            {"id": "other_b", "memory_id": "mem_b", "extracted_by_run_id": OTHER_RUN_ID},
            {"id": "manual_c", "memory_id": "mem_c", "extracted_by_run_id": None},
        ],
    )

    deleted, run_rows = await rollback_run(_Backend(conn), RUN_ID)

    assert deleted == 0
    assert run_rows == 1
    assert {row["id"] for row in conn.kg_triples} == {"other_b", "manual_c"}
    assert conn.memories["mem_a"]["triples_extracted_at"] is None
    assert conn.memories["mem_b"]["triples_extracted_at"] is None
    assert conn.memories["mem_c"]["triples_extracted_at"] == "done"


@pytest.mark.asyncio
async def test_rollback_run_resets_zero_triple_processed_memories():
    conn = _Conn(memories=[_memory("mem_zero", triples_extracted_at="done")])
    conn.extract_run_memories.append(
        {
            "run_id": RUN_ID,
            "memory_id": "mem_zero",
            "processed_at": "now",
        }
    )

    deleted, run_rows = await rollback_run(_Backend(conn), RUN_ID)

    assert deleted == 0
    assert run_rows == 1
    assert conn.kg_triples == []
    assert conn.extract_run_memories == []
    assert conn.memories["mem_zero"]["triples_extracted_at"] is None


@pytest.mark.asyncio
async def test_phase_extract_is_namespace_scoped(monkeypatch):
    async def one_triple(_content: str) -> list[ExtractedTriple]:
        return [ExtractedTriple("Alice", "owns", "Project Helios", 0.9)]

    monkeypatch.setattr(
        runner,
        "_extract_triples_from_prose",
        one_triple,
    )
    conn = _Conn(
        run_namespace="A",
        memories=[
            _memory("mem_a", namespace="A", created_offset=0),
            _memory("mem_b", namespace="B", created_offset=1),
        ],
    )

    n = await phase_extract(_Pool(conn), RUN_ID)

    assert n == 1
    assert [row["memory_id"] for row in conn.kg_triples] == ["mem_a"]
    assert conn.memories["mem_a"]["triples_extracted_at"] == "now"
    assert conn.memories["mem_b"]["triples_extracted_at"] is None


@pytest.mark.asyncio
async def test_extract_verify_filters_below_min_confidence(monkeypatch):
    async def fake_muse(_prompt: str, *, task_type: str, **_kwargs) -> str:
        if task_type == "kg_extraction_verification":
            return json.dumps(
                [
                    {"index": 0, "confidence": 0.95},
                    {"index": 1, "confidence": 0.59},
                    {"index": 2, "confidence": 0.60},
                ]
            )
        return json.dumps(
            [
                {"subject": "Alice", "predicate": "owns", "object": "Project Helios", "confidence": 0.9},
                {"subject": "Bob", "predicate": "owns", "object": "Project Helios", "confidence": 0.9},
                {"subject": "Helios", "predicate": "launches_in", "object": "April", "confidence": 0.9},
            ]
        )

    monkeypatch.setattr(runner, "_call_morpheus_muse", fake_muse)
    conn = _Conn(run_config={"extract": True, "extract_verify": True}, memories=[_memory("mem_verify")])

    n = await phase_extract(_Pool(conn), RUN_ID)

    assert n == 2
    assert [(row["subject"], row["confidence"]) for row in conn.kg_triples] == [
        ("Alice", 0.95),
        ("Helios", 0.60),
    ]


@pytest.mark.asyncio
async def test_malformed_verifier_response_leaves_memory_retryable(monkeypatch):
    async def fake_muse(_prompt: str, *, task_type: str, **_kwargs) -> str:
        if task_type == "kg_extraction_verification":
            return "not-json"
        return json.dumps([{"subject": "Alice", "predicate": "owns", "object": "Helios", "confidence": 0.9}])

    monkeypatch.setattr(runner, "_call_morpheus_muse", fake_muse)
    conn = _Conn(run_config={"extract": True, "extract_verify": True}, memories=[_memory("mem_verify_bad")])

    n = await phase_extract(_Pool(conn), RUN_ID)

    assert n == 0
    assert conn.memories["mem_verify_bad"]["triples_extracted_at"] is None
    assert conn.extract_run_memories == []


@pytest.mark.asyncio
async def test_run_dream_inserts_extract_phase_after_synthesise(monkeypatch):
    from mnemos.core import lifecycle as _lifecycle

    calls: list[str] = []

    async def fake_begin_run(*_args, **_kwargs):
        return RUN_ID

    async def fake_set_phase(_pool, _run_id, phase):
        calls.append(f"phase:{phase}")

    async def fake_phase(_pool, _run_id):
        calls.append("phase_fn")
        return 0

    async def fake_extract(_pool, _run_id):
        calls.append("extract_fn")
        return 0

    async def fake_finish(_pool, _run_id):
        calls.append("finish")

    # Item 11a: ``run_dream`` calls ``_get_backend()`` to dispatch
    # ``sweep_orphan_runs`` / ``begin_run`` / etc. through the new
    # ABC. Wire a no-op backend into the lifecycle so those lookups
    # succeed; the lifecycle functions are monkeypatched to fakes
    # above so the backend is never actually exercised.
    monkeypatch.setattr(_lifecycle, "_persistence_backend", object())

    monkeypatch.setattr(runner, "begin_run", fake_begin_run)
    monkeypatch.setattr(runner, "set_phase", fake_set_phase)
    monkeypatch.setattr(runner, "phase_replay", fake_phase)
    monkeypatch.setattr(runner, "phase_cluster", fake_phase)
    monkeypatch.setattr(runner, "phase_synthesise", fake_phase)
    monkeypatch.setattr(runner, "phase_extract", fake_extract)
    monkeypatch.setattr(runner, "finish_run", fake_finish)

    run_id = await runner.run_dream(object(), config={"extract": True})

    assert run_id == RUN_ID
    assert calls == [
        "phase:replay",
        "phase_fn",
        "phase:cluster",
        "phase_fn",
        "phase:synthesise",
        "phase_fn",
        "phase:extract",
        "extract_fn",
        "phase:commit",
        "finish",
    ]


@pytest.mark.asyncio
async def test_phase_extract_processes_old_backlog_but_not_future_rows(monkeypatch):
    monkeypatch.setattr(runner, "_extract_triples_from_prose", _three_triples)
    base = datetime(2026, 5, 2, 12, 0, 0)
    conn = _Conn(
        run_window=(base - timedelta(minutes=1), base + timedelta(minutes=1)),
        memories=[
            _memory("mem_before", created_offset=-10),
            _memory("mem_inside"),
            _memory("mem_after", created_offset=10),
        ],
    )

    await phase_extract(_Pool(conn), RUN_ID)

    assert {row["memory_id"] for row in conn.extract_run_memories} == {"mem_before", "mem_inside"}


@pytest.mark.asyncio
async def test_phase_extract_limit_does_not_strand_backlog_between_runs(monkeypatch):
    monkeypatch.setenv("MNEMOS_MORPHEUS_EXTRACT_MAX_INPUT_COUNT", "2")
    core_config._reset_settings_for_tests()
    monkeypatch.setattr(runner, "_extract_triples_from_prose", _three_triples)
    conn = _Conn(memories=[_memory(f"mem_{i}", created_offset=i) for i in range(4)])

    await phase_extract(_Pool(conn), RUN_ID)

    assert {row["memory_id"] for row in conn.extract_run_memories} == {"mem_0", "mem_1"}

    # Advance the next replay window past the capped-out rows. The durable
    # per-row extraction marker, not this moving lower bound, drives backlog.
    conn.run_row["window_started_at"] = datetime(2026, 5, 3, 12, 0, 0)
    await phase_extract(_Pool(conn), RUN_ID)

    assert {row["memory_id"] for row in conn.extract_run_memories} == {
        "mem_0",
        "mem_1",
        "mem_2",
        "mem_3",
    }


@pytest.mark.asyncio
async def test_phase_extract_dead_letters_poison_batch_then_processes_newer_rows(monkeypatch):
    monkeypatch.setenv("MNEMOS_MORPHEUS_EXTRACT_MAX_INPUT_COUNT", "2")
    monkeypatch.setenv("MNEMOS_MORPHEUS_EXTRACT_MAX_FAILURES", "2")
    core_config._reset_settings_for_tests()
    attempted: list[str] = []

    async def fail_oldest(content: str) -> list[ExtractedTriple]:
        memory_id = content.split()[0]
        attempted.append(memory_id)
        if memory_id in {"mem_0", "mem_1"}:
            raise runner.MorpheusExtractionError("permanent provider failure")
        return []

    monkeypatch.setattr(runner, "_extract_triples_from_prose", fail_oldest)
    conn = _Conn(memories=[_memory(f"mem_{i}", created_offset=i) for i in range(4)])
    pool = _Pool(conn)

    for _ in range(3):
        await phase_extract(pool, RUN_ID)

    assert attempted == ["mem_0", "mem_1", "mem_0", "mem_1", "mem_2", "mem_3"]
    assert {memory_id: row["status"] for memory_id, row in conn.extract_failures.items()} == {
        "mem_0": "dead_letter",
        "mem_1": "dead_letter",
    }
    assert conn.memories["mem_0"]["triples_extracted_at"] is None
    assert conn.memories["mem_1"]["triples_extracted_at"] is None
    assert conn.memories["mem_2"]["triples_extracted_at"] == "now"
    assert conn.memories["mem_3"]["triples_extracted_at"] == "now"
