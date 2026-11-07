"""Characterisation tests for ``mnemos/domain/portability/phases.py``.

``phases.py`` (609 lines) holds the four MPF sidecar import phases that
``mnemos/domain/portability/import_.py`` drives:

* ``_import_kg_triples``
* ``_import_memory_versions``
* ``_import_compression_manifest``
* ``_restore_memory_branches``

plus the ``_bump`` counter helper. Before this file the module had **zero**
test references: every existing portability test reaches it only indirectly,
through the ``/v1/import`` route, so the per-entry branch logic (owner
rewriting, allowlist gating, parent validation, ``INSERT 0 0`` conflict
verification, secret redaction, winner-pointer degradation) was never pinned
on its own.

These tests PIN today's behaviour -- normal, edge and error paths -- using
the suite's existing ``ConnBackedBackend``/``_FakeTx`` test doubles over a
self-contained mock connection. No network, no live database, no temp state
beyond the mock. They are a characterisation suite, not a design proposal: if
a value changes intentionally, update the pin together with the change.

One defect is pinned as ``xfail(strict=True)`` -- see
``test_mv_conflict_absent_commit_hash_is_rejected_even_when_identical``.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from mnemos.core.ids import caller_scoped_uuid
from mnemos.domain.portability.phases import (
    _bump,
    _import_compression_manifest,
    _import_kg_triples,
    _import_memory_versions,
    _restore_memory_branches,
)
from mnemos.domain.portability.schemas import ImportStats
from tests._charon_fake_backend import ConnBackedBackend, _FakeTx

# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


class _Conn:
    """Self-contained mock asyncpg connection for the phase tests.

    Routing is by SQL substring, exactly like the ``_Conn`` in
    ``tests/test_portability.py``. ``execute`` returns ``execute_result``
    (default ``"INSERT 0 1"``) so a test can flip it to ``"INSERT 0 0"`` to
    exercise the ``ON CONFLICT DO NOTHING`` conflict-verification path.
    """

    def __init__(self, *, execute_result: str = "INSERT 0 1"):
        self.execute_result = execute_result
        self.routed: dict[str, list] = {}
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.executes: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        for needle, payload in self.routed.items():
            if needle in sql:
                return payload
        return []

    async def fetchrow(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        for needle, payload in self.routed.items():
            if needle in sql:
                return payload[0] if payload else None
        return None

    async def fetchval(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        for needle, payload in self.routed.items():
            if needle in sql:
                return payload[0] if payload else None
        return None

    async def execute(self, sql: str, *args):
        self.executes.append((sql, args))
        return self.execute_result


def _backend(conn: _Conn) -> tuple[ConnBackedBackend, _FakeTx]:
    """Build the (backend, tx) pair the phase functions take."""
    return ConnBackedBackend(conn), _FakeTx(conn)


def _stats() -> ImportStats:
    return ImportStats(imported=0, skipped=0, failed=0)


# ---------------------------------------------------------------------------
# Entry builders
# ---------------------------------------------------------------------------


def _kg(
    *,
    triple_id: str = "t1",
    predicate: str = "rel",
    subject: str = "subj",
    obj: str | None = "obj",
    memory_id: str | None = None,
    owner_id: str | None = None,
    namespace: str | None = None,
    **extra,
) -> dict:
    entry = {"id": triple_id, "predicate": predicate, "subject_literal": subject}
    if obj is not None:
        entry["object_literal"] = obj
    if memory_id is not None:
        entry["memory_id"] = memory_id
    if owner_id is not None:
        entry["owner_id"] = owner_id
    if namespace is not None:
        entry["namespace"] = namespace
    entry.update(extra)
    return entry


def _mv(
    *,
    version_id: str = "v1",
    record_id: str = "mem1",
    version_num: int = 1,
    content: str = "hello",
    **extra,
) -> dict:
    entry = {"id": version_id, "record_id": record_id, "version_num": version_num, "content": content}
    entry.update(extra)
    return entry


def _cm(
    *,
    record_id: str = "mem1",
    engine_id: str | None = "apollo",
    compressed_content: str | None = "hello",
    **extra,
) -> dict:
    entry: dict = {"record_id": record_id}
    if engine_id is not None:
        entry["engine_id"] = engine_id
    if compressed_content is not None:
        entry["compressed_content"] = compressed_content
    entry.update(extra)
    return entry


def _derived(envelope_id: str, *, extra: str, owner: str = "alice", ns: str = "alice-ns") -> str:
    return caller_scoped_uuid(caller_owner=owner, caller_namespace=ns, envelope_id=envelope_id, extra=extra)


# ---------------------------------------------------------------------------
# _bump
# ---------------------------------------------------------------------------


def test_bump_starts_at_one_and_increments():
    counter: dict[str, int] = {}
    _bump(counter, "kg_triples")
    _bump(counter, "kg_triples")
    _bump(counter, "memory_versions")
    assert counter == {"kg_triples": 2, "memory_versions": 1}


# ---------------------------------------------------------------------------
# _import_kg_triples -- normal
# ---------------------------------------------------------------------------


def test_kg_non_root_derives_scoped_id_and_imports():
    """preserve_owner=False rewrites the triple id to a caller-scoped UUID."""
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_kg_triples(
            backend,
            tx,
            [_kg()],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={},
        )
    )
    assert stats.sidecars_imported == {"kg_triples": 1}
    assert stats.sidecars_failed == {}
    sql, args = conn.executes[0]
    assert "INSERT INTO kg_triples" in sql
    # index 0 is triple_id (see _INSERT_KG_ORDER in the fake backend).
    assert args[0] == _derived("t1", extra="kg_triples")
    # Non-root stamps the caller's owner + namespace (indices 11, 12).
    assert args[11] == "alice"
    assert args[12] == "alice-ns"


def test_kg_root_preserve_owner_keeps_envelope_id_and_owner():
    """preserve_owner=True keeps the envelope id verbatim and uses the
    entry's stated owner/namespace."""
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_kg_triples(
            backend,
            tx,
            [_kg(memory_id="mem1", owner_id="bob", namespace="bob-ns")],
            caller_user_id="admin",
            caller_namespace="default",
            preserve_owner=True,
            stats=stats,
            allowlist={"mem1": ("bob", "bob-ns")},
        )
    )
    assert stats.sidecars_imported == {"kg_triples": 1}
    _, args = conn.executes[0]
    assert args[0] == "t1"  # id not rewritten
    assert args[11] == "bob"
    assert args[12] == "bob-ns"


def test_kg_missing_object_defaults_to_empty_string():
    """A triple with a subject but no object literal/id binds obj='' rather
    than failing (object is optional in the KG schema)."""
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_kg_triples(
            backend,
            tx,
            [_kg(obj=None)],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={},
        )
    )
    assert stats.sidecars_imported == {"kg_triples": 1}
    _, args = conn.executes[0]
    assert args[3] == ""  # obj index


# ---------------------------------------------------------------------------
# _import_kg_triples -- error paths
# ---------------------------------------------------------------------------


def test_kg_metadata_present_raises_415():
    """A kg_triple carrying metadata is rejected with HTTP 415 BEFORE any
    record is committed (fail the whole migration, per the inline comment)."""
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            _import_kg_triples(
                backend,
                tx,
                [_kg(metadata={"graph": "cognee"})],
                caller_user_id="alice",
                caller_namespace="alice-ns",
                preserve_owner=False,
                stats=stats,
                allowlist={},
            )
        )
    assert exc.value.status_code == 415
    assert "metadata is not supported" in exc.value.detail
    assert conn.executes == []  # nothing inserted


def test_kg_missing_predicate_fails_without_insert():
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    entry = _kg()
    entry.pop("predicate")
    asyncio.run(
        _import_kg_triples(
            backend,
            tx,
            [entry],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={},
        )
    )
    assert stats.sidecars_failed == {"kg_triples": 1}
    assert any("missing required" in e for e in stats.errors)
    assert conn.executes == []


def test_kg_missing_subject_fails():
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    entry = {"id": "t1", "predicate": "rel"}  # no subject_literal / subject_id
    asyncio.run(
        _import_kg_triples(
            backend,
            tx,
            [entry],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={},
        )
    )
    assert stats.sidecars_failed == {"kg_triples": 1}
    assert any("missing subject_literal/subject_id" in e for e in stats.errors)
    assert conn.executes == []


def test_kg_memory_id_not_in_allowlist_fails():
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_kg_triples(
            backend,
            tx,
            [_kg(memory_id="nope")],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=True,
            stats=stats,
            allowlist={"real": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_failed == {"kg_triples": 1}
    assert any("not in caller-owned memory id set" in e for e in stats.errors)
    assert conn.executes == []


def test_kg_memory_id_owner_mismatch_fails():
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_kg_triples(
            backend,
            tx,
            [_kg(memory_id="m1", owner_id="alice", namespace="alice-ns")],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=True,
            stats=stats,
            allowlist={"m1": ("bob", "alice-ns")},
        )
    )
    assert stats.sidecars_failed == {"kg_triples": 1}
    assert any("belongs to owner 'bob'" in e for e in stats.errors)


def test_kg_memory_id_namespace_mismatch_fails():
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_kg_triples(
            backend,
            tx,
            [_kg(memory_id="m1", owner_id="alice", namespace="alice-ns")],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=True,
            stats=stats,
            allowlist={"m1": ("alice", "other-ns")},
        )
    )
    assert stats.sidecars_failed == {"kg_triples": 1}
    assert any("is in namespace 'other-ns'" in e for e in stats.errors)


def test_kg_insert_exception_marks_failed_and_logs():
    class _BoomConn(_Conn):
        async def execute(self, sql, *args):  # noqa: ARG002
            raise RuntimeError("boom")

    conn = _BoomConn()
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_kg_triples(
            backend,
            tx,
            [_kg()],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={},
        )
    )
    assert stats.sidecars_failed == {"kg_triples": 1}
    assert any("RuntimeError: boom" in e for e in stats.errors)


# ---------------------------------------------------------------------------
# _import_kg_triples -- INSERT 0 0 conflict verification
# ---------------------------------------------------------------------------


_KG_EXISTING = {
    "subject": "subj",
    "predicate": "rel",
    "object": "obj",
    "subject_type": None,
    "object_type": None,
    "memory_id": None,
    "confidence": 1.0,
    "owner_id": "alice",
    "namespace": "alice-ns",
    "valid_from": None,
    "valid_until": None,
    "created": None,
}


def test_kg_conflict_matching_row_skipped():
    conn = _Conn(execute_result="INSERT 0 0")
    conn.routed = {"FROM kg_triples WHERE id": [dict(_KG_EXISTING)]}
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_kg_triples(
            backend,
            tx,
            [_kg()],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={},
        )
    )
    assert stats.sidecars_skipped == {"kg_triples": 1}
    assert stats.sidecars_failed == {}


def test_kg_conflict_mismatched_row_failed():
    existing = dict(_KG_EXISTING)
    existing["subject"] = "other-subject"
    conn = _Conn(execute_result="INSERT 0 0")
    conn.routed = {"FROM kg_triples WHERE id": [existing]}
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_kg_triples(
            backend,
            tx,
            [_kg()],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={},
        )
    )
    assert stats.sidecars_failed == {"kg_triples": 1}
    assert any("existing row doesn't match envelope claim" in e for e in stats.errors)


def test_kg_conflict_existing_none_failed():
    """A conflict with no readable existing row is treated as a mismatch."""
    conn = _Conn(execute_result="INSERT 0 0")
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_kg_triples(
            backend,
            tx,
            [_kg()],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={},
        )
    )
    assert stats.sidecars_failed == {"kg_triples": 1}
    assert any("existing row doesn't match envelope claim" in e for e in stats.errors)


def test_kg_conflict_not_fresh_tolerates_absent_created():
    """When the referenced memory is NOT freshly inserted and the envelope
    omits ``created``, a differing stored ``created`` is tolerated (skip)."""
    existing = dict(_KG_EXISTING)
    existing["created"] = datetime(2020, 1, 1, tzinfo=timezone.utc)
    conn = _Conn(execute_result="INSERT 0 0")
    conn.routed = {"FROM kg_triples WHERE id": [existing]}
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_kg_triples(
            backend,
            tx,
            [_kg()],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={},
            inserted_record_ids=set(),  # memory NOT freshly inserted
        )
    )
    assert stats.sidecars_skipped == {"kg_triples": 1}
    assert stats.sidecars_failed == {}


def test_kg_conflict_fresh_memory_rejects_absent_created_mismatch():
    """When the referenced memory WAS freshly inserted, the ``created``
    tolerance is disabled: an absent envelope ``created`` must match the
    stored value exactly, so a differing stored ``created`` is rejected."""
    existing = dict(_KG_EXISTING)
    existing["created"] = datetime(2020, 1, 1, tzinfo=timezone.utc)
    conn = _Conn(execute_result="INSERT 0 0")
    conn.routed = {"FROM kg_triples WHERE id": [existing]}
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_kg_triples(
            backend,
            tx,
            [_kg(memory_id="m1")],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={"m1": ("alice", "alice-ns")},
            inserted_record_ids={"m1"},  # memory WAS freshly inserted
        )
    )
    assert stats.sidecars_failed == {"kg_triples": 1}
    assert any("existing row doesn't match envelope claim" in e for e in stats.errors)


def test_kg_conflict_absent_confidence_defaults_to_one():
    """An envelope that omits ``confidence`` is compared against the schema
    default of 1.0 (the kg_triples column defaults to 1.0)."""
    existing = dict(_KG_EXISTING)
    existing["confidence"] = 1.0
    conn = _Conn(execute_result="INSERT 0 0")
    conn.routed = {"FROM kg_triples WHERE id": [existing]}
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_kg_triples(
            backend,
            tx,
            [_kg()],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={},
        )
    )
    assert stats.sidecars_skipped == {"kg_triples": 1}


# ---------------------------------------------------------------------------
# _restore_memory_branches
# ---------------------------------------------------------------------------


def test_restore_no_memory_ids_returns_early():
    conn = _Conn()
    backend, tx = _backend(conn)
    asyncio.run(_restore_memory_branches(backend, tx, []))
    assert conn.fetch_calls == []
    assert conn.executes == []


def test_restore_empty_authorized_uuids_returns_early():
    """An explicitly-empty authorization list short-circuits (no fetch)."""
    conn = _Conn()
    backend, tx = _backend(conn)
    asyncio.run(_restore_memory_branches(backend, tx, ["m1"], authorized_version_uuids=[]))
    assert conn.fetch_calls == []
    assert conn.executes == []


def test_restore_upserts_each_branch_head():
    conn = _Conn()
    conn.routed = {
        "DISTINCT ON (memory_id, branch)": [
            {"memory_id": "m1", "branch": "main", "head_version_id": "v9"},
            {"memory_id": "m1", "branch": "dev", "head_version_id": "v8"},
        ]
    }
    backend, tx = _backend(conn)
    asyncio.run(_restore_memory_branches(backend, tx, ["m1"], authorized_version_uuids=["v9", "v8"]))
    upserts = [e for e in conn.executes if "memory_branches" in e[0]]
    assert len(upserts) == 2
    assert upserts[0][1] == ("m1", "main", "v9")
    assert upserts[1][1] == ("m1", "dev", "v8")


# ---------------------------------------------------------------------------
# _import_memory_versions -- normal
# ---------------------------------------------------------------------------


def test_mv_non_root_rewrites_id_parent_merge_and_commit_hash():
    """preserve_owner=False rewrites version id, parent, merge_parents (all
    caller-scoped) and re-hashes commit_hash with owner+namespace."""
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    sidecar = [
        _mv(version_id="v0", version_num=1, content="a"),
        _mv(
            version_id="v1",
            version_num=2,
            content="b",
            parent_version_id="v0",
            merge_parents=["v0"],
            commit_hash="abc123",
        ),
    ]
    asyncio.run(
        _import_memory_versions(
            backend,
            tx,
            sidecar,
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_imported == {"memory_versions": 2}
    # The v1 insert carries a parent (index 19 non-None).
    v1 = next(e for e in conn.executes if e[1][19] is not None)
    args = v1[1]
    assert args[0] == _derived("v1", extra="memory_versions")
    assert args[19] == _derived("v0", extra="memory_versions")
    assert args[21] == [_derived("v0", extra="memory_versions")]
    expected_ch = hashlib.sha256(b"\x00".join([b"alice", b"alice-ns", b"abc123"])).hexdigest()
    assert args[18] == expected_ch


def test_mv_root_preserve_owner_keeps_id_and_commit_verbatim():
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_memory_versions(
            backend,
            tx,
            [_mv(commit_hash="abc123", owner_id="alice", namespace="alice-ns")],
            caller_user_id="admin",
            caller_namespace="default",
            preserve_owner=True,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_imported == {"memory_versions": 1}
    _, args = conn.executes[0]
    assert args[0] == "v1"  # id verbatim
    assert args[18] == "abc123"  # commit_hash verbatim
    assert args[8] == "alice"
    assert args[9] == "alice-ns"


def test_mv_imports_version_and_returns_authorized_sets():
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    authorized, failed, auth_versions = asyncio.run(
        _import_memory_versions(
            backend,
            tx,
            [_mv()],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=True,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_imported == {"memory_versions": 1}
    assert authorized == {"mem1"}
    assert failed == set()
    assert auth_versions == {"v1"}


# ---------------------------------------------------------------------------
# _import_memory_versions -- error paths
# ---------------------------------------------------------------------------


def test_mv_missing_required_field_fails():
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_memory_versions(
            backend,
            tx,
            [_mv(content="")],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=True,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_failed == {"memory_versions": 1}
    assert any("missing required field 'content'" in e for e in stats.errors)
    assert conn.executes == []


def test_mv_allowlist_rejection():
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    authorized, failed, _ = asyncio.run(
        _import_memory_versions(
            backend,
            tx,
            [_mv()],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=True,
            stats=stats,
            allowlist={},
        )
    )
    assert stats.sidecars_failed == {"memory_versions": 1}
    assert authorized == set()
    assert failed == {"mem1"}
    assert conn.executes == []


def test_mv_duplicate_id_raises_value_error():
    """``_topo_sort_versions`` rejects duplicate ids outright (fail-closed)."""
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    with pytest.raises(ValueError, match="duplicate id"):
        asyncio.run(
            _import_memory_versions(
                backend,
                tx,
                [_mv(version_id="v1", version_num=1), _mv(version_id="v1", version_num=2)],
                caller_user_id="alice",
                caller_namespace="alice-ns",
                preserve_owner=True,
                stats=stats,
                allowlist={},
            )
        )


def test_mv_parent_foreign_record_rejected():
    conn = _Conn()
    # The parent exists in the DB but belongs to a different record.
    conn.routed = {
        "FROM memory_versions WHERE id = ANY": [
            {"id": "v0", "memory_id": "other", "owner_id": "alice", "namespace": "alice-ns"}
        ]
    }
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_memory_versions(
            backend,
            tx,
            [_mv(parent_version_id="v0")],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=True,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_failed == {"memory_versions": 1}
    assert any("foreign-tenant or foreign-record" in e for e in stats.errors)


def test_mv_insert_exception_marks_failed():
    class _BoomConn(_Conn):
        async def execute(self, sql, *args):  # noqa: ARG002
            raise RuntimeError("boom")

    conn = _BoomConn()
    backend, tx = _backend(conn)
    stats = _stats()
    _, failed, _ = asyncio.run(
        _import_memory_versions(
            backend,
            tx,
            [_mv()],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=True,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_failed == {"memory_versions": 1}
    assert failed == {"mem1"}
    assert any("RuntimeError: boom" in e for e in stats.errors)


def test_mv_malformed_snapshot_at_marks_failed():
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_memory_versions(
            backend,
            tx,
            [_mv(snapshot_at="not-a-time")],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=True,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_failed == {"memory_versions": 1}
    assert any("MalformedTimestampError" in e for e in stats.errors)


# ---------------------------------------------------------------------------
# _import_memory_versions -- INSERT 0 0 conflict verification
# ---------------------------------------------------------------------------


_MV_EXISTING = {
    "memory_id": "mem1",
    "owner_id": "alice",
    "namespace": "alice-ns",
    "version_num": 1,
    "content": "hello",
    "commit_hash": "abc123",
    "parent_version_id": None,
    "branch": "main",
    "merge_parents": None,
    "category": None,
    "subcategory": None,
    "metadata": {},
    "verbatim_content": None,
    "permission_mode": 600,
    "source_model": None,
    "source_provider": None,
    "source_session": None,
    "source_agent": None,
    "snapshot_at": None,
    "snapshot_by": None,
    "change_type": "create",
}


def test_mv_conflict_matching_row_skipped():
    conn = _Conn(execute_result="INSERT 0 0")
    conn.routed = {"FROM memory_versions WHERE id = $1::uuid": [dict(_MV_EXISTING)]}
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_memory_versions(
            backend,
            tx,
            [_mv(commit_hash="abc123")],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=True,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_skipped == {"memory_versions": 1}
    assert stats.sidecars_failed == {}


def test_mv_conflict_mismatched_row_failed():
    existing = dict(_MV_EXISTING)
    existing["content"] = "different"
    conn = _Conn(execute_result="INSERT 0 0")
    conn.routed = {"FROM memory_versions WHERE id = $1::uuid": [existing]}
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_memory_versions(
            backend,
            tx,
            [_mv(commit_hash="abc123")],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=True,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_failed == {"memory_versions": 1}
    assert any("existing row doesn't match envelope claim" in e for e in stats.errors)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT: an envelope memory_version that omits commit_hash (a "
        "non-required field) can never be idempotently re-imported. On the "
        "INSERT 0 0 conflict path, phases.py guards with "
        "`or (not entry.get('commit_hash') or existing['commit_hash'] != "
        "entry['commit_hash'])`, so an ABSENT commit_hash short-circuits to "
        "'mismatch' regardless of the stored row -- even when the row is "
        "byte-for-byte identical (both sides NULL). The sibling tolerances "
        "(snapshot_at / created / valid_from) explicitly allow an absent "
        "value; commit_hash has no such tolerance. commit_hash is nullable "
        "on the default SQLite backend (migrations_sqlite/migrations.sql "
        "line 273), so the mismatched-idempotent-import path is reachable "
        "there. Reproduced 2026-11-05 against phases.py@782ed312."
    ),
)
def test_mv_conflict_absent_commit_hash_is_rejected_even_when_identical():
    """EXPECTED (correct) behaviour: an envelope entry with no commit_hash
    whose stored row is otherwise identical should be SKIPPED as an
    idempotent match. Today it is REJECTED -- see the xfail reason."""
    existing = dict(_MV_EXISTING)
    existing["commit_hash"] = None
    conn = _Conn(execute_result="INSERT 0 0")
    conn.routed = {"FROM memory_versions WHERE id = $1::uuid": [existing]}
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_memory_versions(
            backend,
            tx,
            [_mv()],  # no commit_hash
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=True,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    # Correct: identical row -> skipped. (Actual: rejected -> this fails.)
    assert stats.sidecars_skipped == {"memory_versions": 1}
    assert stats.sidecars_failed == {}


# ---------------------------------------------------------------------------
# _import_compression_manifest -- normal
# ---------------------------------------------------------------------------


def test_cm_imports_variant():
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_compression_manifest(
            backend,
            tx,
            [_cm()],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_imported == {"compression_manifest": 1}
    sql, args = conn.executes[0]
    assert "INSERT INTO memory_compressed_variants" in sql
    assert args[0] == "mem1"  # memory_id
    assert args[1] == "alice"  # owner_id (non-root)
    assert args[3] == "apollo"  # engine_id
    assert args[5] == "hello"  # compressed_content


def test_cm_secret_content_redacted():
    """A compressed_content that the classifier vaults is replaced with
    ``[REDACTED]`` before it reaches the insert."""
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_compression_manifest(
            backend,
            tx,
            [_cm(compressed_content="api_key=sk-1234567890abcdef")],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_imported == {"compression_manifest": 1}
    _, args = conn.executes[0]
    assert args[5] == "[REDACTED]"


# ---------------------------------------------------------------------------
# _import_compression_manifest -- winner-pointer handling
# ---------------------------------------------------------------------------


def test_cm_invalid_winner_uuid_nulled():
    """A non-UUID winner_contest_id parses to None (no candidate lookup)."""
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_compression_manifest(
            backend,
            tx,
            [_cm(winner_contest_id="not-a-uuid")],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_imported == {"compression_manifest": 1}
    assert stats.errors == []
    _, args = conn.executes[0]
    assert args[2] is None  # winner_candidate_id


def test_cm_winner_candidate_present_kept():
    conn = _Conn()
    winner = "11111111-1111-1111-1111-111111111111"
    conn.routed = {"FROM memory_compression_candidates WHERE id": [1]}
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_compression_manifest(
            backend,
            tx,
            [_cm(winner_contest_id=winner)],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_imported == {"compression_manifest": 1}
    _, args = conn.executes[0]
    assert args[2] == winner  # winner preserved


def test_cm_winner_candidate_absent_degrades_and_records_error():
    """A winner whose candidate row is missing is DEGRADED to NULL (row is
    still imported) and the degradation is recorded in stats.errors."""
    conn = _Conn()
    winner = "11111111-1111-1111-1111-111111111111"
    # No route for the candidate lookup -> fetchval returns None -> absent.
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_compression_manifest(
            backend,
            tx,
            [_cm(winner_contest_id=winner)],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_imported == {"compression_manifest": 1}
    assert any("references a compression candidate that is not present" in e for e in stats.errors)
    _, args = conn.executes[0]
    assert args[2] is None  # degraded


# ---------------------------------------------------------------------------
# _import_compression_manifest -- error paths
# ---------------------------------------------------------------------------


def test_cm_missing_engine_id_fails():
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_compression_manifest(
            backend,
            tx,
            [_cm(engine_id=None)],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_failed == {"compression_manifest": 1}
    assert any("missing required field 'engine_id'" in e for e in stats.errors)
    assert conn.executes == []


def test_cm_allowlist_rejection():
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_compression_manifest(
            backend,
            tx,
            [_cm(record_id="memX")],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_failed == {"compression_manifest": 1}
    assert any("not in caller-owned memory id set" in e for e in stats.errors)
    assert conn.executes == []


def test_cm_malformed_selected_at_marks_failed():
    conn = _Conn()
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_compression_manifest(
            backend,
            tx,
            [_cm(selected_at="not-a-time")],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_failed == {"compression_manifest": 1}
    assert any("MalformedTimestampError" in e for e in stats.errors)


# ---------------------------------------------------------------------------
# _import_compression_manifest -- INSERT 0 0 conflict verification
# ---------------------------------------------------------------------------


_CM_EXISTING = {
    "owner_id": "alice",
    "winner_candidate_id": None,
    "engine_id": "apollo",
    "engine_version": None,
    "compressed_content": "hello",
    "compressed_tokens": None,
    "compression_ratio": None,
    "quality_score": None,
    "composite_score": None,
    "scoring_profile": "balanced",
    "judge_model": None,
    "selected_at": None,
}


def test_cm_conflict_matching_row_skipped():
    conn = _Conn(execute_result="INSERT 0 0")
    conn.routed = {"FROM memory_compressed_variants WHERE memory_id": [dict(_CM_EXISTING)]}
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_compression_manifest(
            backend,
            tx,
            [_cm()],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_skipped == {"compression_manifest": 1}
    assert stats.sidecars_failed == {}


def test_cm_conflict_mismatched_row_failed():
    existing = dict(_CM_EXISTING)
    existing["engine_id"] = "other-engine"
    conn = _Conn(execute_result="INSERT 0 0")
    conn.routed = {"FROM memory_compressed_variants WHERE memory_id": [existing]}
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_compression_manifest(
            backend,
            tx,
            [_cm()],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_failed == {"compression_manifest": 1}
    assert any("existing variant doesn't match envelope claim" in e for e in stats.errors)


def test_cm_conflict_existing_none_failed():
    conn = _Conn(execute_result="INSERT 0 0")
    backend, tx = _backend(conn)
    stats = _stats()
    asyncio.run(
        _import_compression_manifest(
            backend,
            tx,
            [_cm()],
            caller_user_id="alice",
            caller_namespace="alice-ns",
            preserve_owner=False,
            stats=stats,
            allowlist={"mem1": ("alice", "alice-ns")},
        )
    )
    assert stats.sidecars_failed == {"compression_manifest": 1}
    assert any("existing variant doesn't match envelope claim" in e for e in stats.errors)
