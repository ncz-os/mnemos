"""Characterisation tests (FB73) for the three least-tested large modules.

Targets, chosen by package size vs tests/ import references on 2026-10-31
(master b38624f):

- ``mnemos/core/resilience.py``      (2203 lines, 2 test files referencing)
- ``mnemos/tools/memory_import.py``  (1449 lines, 3 test files referencing)
- ``mnemos/portability/charon.py``   (1335 lines, 3 test files referencing)

These tests PIN today's behaviour of the public, dependency-free surface of
those modules (including error paths). They are not a design proposal: if a
value changes intentionally, update the pin together with the change.

Deliberately NOT exercised here (they need live brokers):
``NatsCircuitBreaker``, ``RedisCircuitBreakerPool``, ``RedisRateLimiterPool``.
Only the pure helper functions around the NATS path are pinned.

One suspected defect is pinned as ``xfail(strict=True)`` — see the
``XFAIL sidecar trailer is silently dropped`` test for the reasoning.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import uuid
from pathlib import Path

import pytest

from mnemos.core.resilience import (
    CircuitState,
    InProcessCircuitBreaker,
    InProcessCircuitBreakerPool,
    _nats_entry_revision,
    _nats_entry_value,
    _nats_missing_key,
    _nats_wrong_revision,
    call_maybe_async,
    maybe_await,
)
from mnemos.portability import charon
from mnemos.tools.memory_import import BaseImporter, JsonImporter, _stable_id_from_mem

pytest.importorskip("jsonschema", reason="charon export validates against the MIF schema")

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_CONFORMANT_MEMORY = {
    "id": "mem_1782675392144_3ba026",
    "content": "MNEMOS adopts MIF natively.",
    "category": "decisions",
    "namespace": "default",
    "created": "2026-06-28T20:00:00+00:00",
}
_MPF_RECORD = {
    "id": "rec1",
    "kind": "memory",
    "payload_version": "mnemos-3.1",
    "payload": {"content": "mpf line"},
}
_SIDECAR_TRAILER = {"mpf_sidecars": True, "kg_triples": [{"subject": "s", "predicate": "p", "object": "o"}]}


def _dry(stats_only):
    """Run an importer under dry_run, capturing stdout, and return (stats, out)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        stats = stats_only()
    return stats, buf.getvalue()


# ---------------------------------------------------------------------------
# mnemos.core.resilience — InProcessCircuitBreaker
# ---------------------------------------------------------------------------


class TestInProcessCircuitBreaker:
    def test_a_new_breaker_is_closed_and_allowed_with_a_status_shape(self):
        cb = InProcessCircuitBreaker("prov", failure_threshold=2, cooldown_seconds=0, success_threshold=2)
        assert cb.state is CircuitState.CLOSED
        assert cb.is_allowed() is True
        assert cb.status() == {"state": "closed", "failures": 0}

    def test_failures_open_the_circuit_at_the_threshold(self):
        cb = InProcessCircuitBreaker("prov", failure_threshold=2, cooldown_seconds=3600)
        cb.record_failure()
        assert cb.is_allowed() is True
        assert cb.status() == {"state": "closed", "failures": 1}
        cb.record_failure()
        assert cb.state is CircuitState.OPEN
        assert cb.check_open() is True
        assert cb.is_allowed() is False
        assert cb.status() == {"state": "open", "failures": 2}

    def test_open_breaker_records_success_but_stays_open_before_cooldown(self):
        cb = InProcessCircuitBreaker("prov", failure_threshold=1, cooldown_seconds=3600)
        cb.record_failure()
        assert cb.is_allowed() is False
        cb.record_success()
        assert cb.state is CircuitState.OPEN
        assert cb.status() == {"state": "open", "failures": 1}

    def test_an_elapsed_cooldown_moves_open_to_half_open_and_allows(self):
        cb = InProcessCircuitBreaker("prov", failure_threshold=1, cooldown_seconds=0)
        cb.record_failure()
        assert cb.state is CircuitState.OPEN
        allowed = cb.is_allowed()
        assert allowed is True
        assert cb.state is CircuitState.HALF_OPEN

    def test_half_open_closes_after_success_threshold_successes(self):
        cb = InProcessCircuitBreaker("prov", failure_threshold=1, cooldown_seconds=0, success_threshold=2)
        cb.record_failure()
        assert cb.is_allowed() is True  # cooldown elapsed -> half open
        assert cb.state is CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state is CircuitState.HALF_OPEN  # one probe success is not enough
        cb.record_success()
        assert cb.state is CircuitState.CLOSED
        assert cb.status() == {"state": "closed", "failures": 0}

    def test_half_open_reopens_when_failures_reach_the_threshold_again(self):
        cb = InProcessCircuitBreaker("prov", failure_threshold=1, cooldown_seconds=0, success_threshold=2)
        cb.record_failure()
        assert cb.is_allowed() is True
        assert cb.state is CircuitState.HALF_OPEN
        cb.record_failure()
        assert cb.state is CircuitState.OPEN

    def test_half_open_is_allowed_unconditionally(self):
        cb = InProcessCircuitBreaker("prov", failure_threshold=5, cooldown_seconds=3600)
        cb.state = CircuitState.HALF_OPEN  # hand-placed; no constructor route
        assert cb.is_allowed() is True
        assert cb.state is CircuitState.HALF_OPEN

    def test_closed_record_success_decrements_the_failure_count(self):
        cb = InProcessCircuitBreaker("prov", failure_threshold=5, cooldown_seconds=300, success_threshold=1)
        cb.record_failure()
        cb.record_failure()
        assert cb.status() == {"state": "closed", "failures": 2}
        cb.record_success()
        assert cb.status() == {"state": "closed", "failures": 1}
        cb.record_success()
        assert cb.status() == {"state": "closed", "failures": 0}

    def test_check_open_is_the_negation_of_is_allowed(self):
        cb = InProcessCircuitBreaker("prov", failure_threshold=1, cooldown_seconds=3600)
        assert cb.check_open() is False
        cb.record_failure()
        assert cb.check_open() is True

    def test_a_dropped_failure_indicator_is_never_negative(self):
        cb = InProcessCircuitBreaker("prov", failure_threshold=5, cooldown_seconds=300, success_threshold=1)
        cb.record_failure()
        cb.record_success()
        cb.record_success()  # one decrement too many
        assert cb.status() == {"state": "closed", "failures": 0}


class TestInProcessCircuitBreakerPool:
    def test_providers_get_independent_breakers_lazily(self):
        pool = InProcessCircuitBreakerPool(failure_threshold=1, cooldown_seconds=3600)
        assert pool.status() == {}
        assert pool.is_allowed("beta") is True
        assert set(pool.status()) == {"beta"}
        assert pool.is_allowed("beta") is True

    def test_an_open_provider_blocks_only_itself(self):
        pool = InProcessCircuitBreakerPool(failure_threshold=1, cooldown_seconds=3600)
        pool.record_failure("alpha")
        assert pool.is_allowed("alpha") is False
        assert pool.is_allowed("alpha") is False
        # capture the pool status before touching beta: is_allowed() CREATES
        # the second breaker lazily, and status() then lists it too.
        assert pool.status() == {"alpha": {"state": "open", "failures": 1}}
        assert pool.is_allowed("beta") is True
        assert pool.status() == {
            "alpha": {"state": "open", "failures": 1},
            "beta": {"state": "closed", "failures": 0},
        }

    def test_status_shape_is_nested_by_provider(self):
        pool = InProcessCircuitBreakerPool(failure_threshold=2, cooldown_seconds=300)
        pool.record_failure("one")
        pool.record_success("one")
        pool.record_success("one")
        assert pool.status() == {"one": {"state": "closed", "failures": 0}}


# ---------------------------------------------------------------------------
# mnemos.core.resilience — NATS helpers and await shims (no broker)
# ---------------------------------------------------------------------------


class TestNatsHelpers:
    def test_missing_key_is_matched_on_message_or_class(self):
        assert _nats_missing_key(ValueError("no keys found")) is True
        assert _nats_missing_key(RuntimeError("NATS: NoKeysError: not found")) is True
        assert _nats_missing_key(ValueError("boom")) is False

    def test_wrong_revision_is_matched_on_message_or_class(self):
        assert _nats_wrong_revision(RuntimeError("wrong last sequence")) is True
        assert _nats_wrong_revision(ValueError("WRONG LAST")) is True
        assert _nats_wrong_revision(ValueError("boom")) is False

    def test_entry_value_coerces_bytes_bytearray_and_text(self):
        assert _nats_entry_value("plain") == b"plain"
        assert _nats_entry_value(b"bytes") == b"bytes"
        assert _nats_entry_value(bytearray(b"arra")) == b"arra"

    def test_entry_revision_prefers_revision_then_rev(self):
        class WithRev:
            rev = 7

        assert _nats_entry_revision(None) is None
        # plain non-entry values (a bare int, an object without revision/rev)
        # read their attributes and fall through to None — the helper is
        # written for NATS JetStream entries, not scalars.
        assert _nats_entry_revision(3) is None
        assert _nats_entry_revision(WithRev()) == 7
        assert _nats_entry_revision(object()) is None


class TestAwaitShims:
    def test_maybe_await_passes_plain_values_through(self):
        assert asyncio.run(maybe_await(5)) == 5
        assert asyncio.run(maybe_await("x")) == "x"

    def test_maybe_awaits_coroutines(self):
        async def _co(value):
            return value

        assert asyncio.run(maybe_await(_co(9))) == 9

    def test_call_maybe_async_works_for_sync_and_async_calls(self):
        def _sync(value):
            return value

        async def _async(value):
            return value + 1

        assert asyncio.run(call_maybe_async(_sync, 1)) == 1
        assert asyncio.run(call_maybe_async(_async, 1)) == 2


# ---------------------------------------------------------------------------
# mnemos.tools.memory_import — helpers and JsonImporter (dry-run only)
# ---------------------------------------------------------------------------


class TestImportHelpers:
    def test_stable_id_is_content_addressed_and_deterministic(self):
        mem = {"id": None, "content": "hello", "category": "x", "tags": ["a"]}
        assert _stable_id_from_mem(mem) == _stable_id_from_mem(dict(mem))
        assert _stable_id_from_mem(mem).startswith("imported_")
        # content-addressed: None fields are dropped from the digest, but a
        # NON-None field (any key) does feed it
        assert _stable_id_from_mem({"content": "one", "id": None}) == _stable_id_from_mem({"content": "one"})
        assert (
            _stable_id_from_mem({"content": "one"})
            != _stable_id_from_mem({"content": "one", "id": "given"})
        )

    def test_scope_overrides_apply_only_when_configured(self):
        plain = BaseImporter()
        assert plain._apply_scope_overrides({"content": "c"}) == {"content": "c"}
        both = BaseImporter(owner_id="o", namespace="n")
        assert both._apply_scope_overrides({"content": "c"}) == {
            "content": "c",
            "owner_id": "o",
            "namespace": "n",
        }
        owner_only = BaseImporter(owner_id="o")
        assert owner_only._apply_scope_overrides({"content": "c", "owner_id": "old"}) == {
            "content": "c",
            "owner_id": "o",
        }
        # caller's dict is copied, not mutated in place
        mem = {"content": "c"}
        owner_only._apply_scope_overrides(mem)
        assert "owner_id" not in mem


class TestJsonImporterWireShapes:
    def _write(self, tmp_path: Path, name: str, text: str) -> Path:
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_plain_array_dry_run_counts_and_skips_empty_content(self, tmp_path):
        f = self._write(
            tmp_path,
            "a.json",
            json.dumps([{"content": "c1", "category": "k1", "tags": ["t"]}, {"content": "", "category": "k"}]),
        )
        stats, out = _dry(JsonImporter(str(f), dry_run=True).run)
        assert stats == {"imported": 1, "failed": 0, "skipped": 1}
        assert "Loaded 1 memories from a.json (1 skipped)" in out
        assert "Result: 1 imported, 0 failed" in out

    def test_wrapped_memories_object_is_unwrapped(self, tmp_path):
        f = self._write(tmp_path, "b.json", json.dumps({"memories": [{"content": "w1"}]}))
        stats, _ = _dry(JsonImporter(str(f), dry_run=True).run)
        assert stats == {"imported": 1, "failed": 0, "skipped": 0}

    def test_malformed_top_level_json_fails_without_raising(self, tmp_path, capsys):
        f = self._write(tmp_path, "c.json", "{not json")
        stats, _ = _dry(JsonImporter(str(f), dry_run=True).run)
        assert stats == {"imported": 0, "failed": 1, "skipped": 0}
        err = capsys.readouterr().err
        assert "ERROR: Cannot parse JSON" in err

    def test_jsonl_is_inferred_from_the_suffix(self, tmp_path):
        f = self._write(tmp_path, "e.jsonl", json.dumps({"content": "flat one"}) + "\n")
        stats, _ = _dry(JsonImporter(str(f), dry_run=True).run)
        assert stats == {"imported": 1, "failed": 0, "skipped": 0}
        almost = self._write(tmp_path, "f.json", json.dumps({"content": "flat one"}) + "\n")
        # a .json file is parsed whole, not line by line: the object lands in
        # the one-shot path, its single value is a string, so the row is skipped
        stats2, _ = _dry(JsonImporter(str(almost), dry_run=True).run)
        assert stats2 == {"imported": 0, "failed": 0, "skipped": 1}

    def test_jsonl_bad_lines_count_as_failures_but_good_lines_still_import(self, tmp_path, capsys):
        f = self._write(
            tmp_path,
            "g.jsonl",
            json.dumps({"content": "flat one"})
            + "\nnot json at all\n\n"
            + json.dumps(_MPF_RECORD)
            + "\n",
        )
        stats, _ = _dry(JsonImporter(str(f), dry_run=True).run)
        assert stats == {"imported": 2, "failed": 1, "skipped": 0}
        assert "WARNING: line 2: bad JSON" in capsys.readouterr().err

    def test_jsonl_mpf_record_lines_are_unwrapped_into_flat_memories(self, tmp_path):
        f = self._write(
            tmp_path,
            "h.jsonl",
            json.dumps(_MPF_RECORD)
            + "\n"
            + json.dumps({"id": "rec2", "kind": "memory", "payload_version": "mnemos-3.1", "payload": {"content": "second"}})
            + "\n",
        )
        stats, _ = _dry(JsonImporter(str(f), dry_run=True).run)
        assert stats == {"imported": 2, "failed": 0, "skipped": 0}

    def test_mpf_envelope_one_shot_imports_its_records(self, tmp_path):
        f = self._write(
            tmp_path,
            "i.json",
            json.dumps({"mpf_version": "0.1.1", "records": [dict(_MPF_RECORD)]}),
        )
        stats, _ = _dry(JsonImporter(str(f), dry_run=True).run)
        assert stats == {"imported": 1, "failed": 0, "skipped": 0}

    def test_jsonl_sidecar_trailer_with_preserve_metadata_builds_the_passthrough_envelope(self, tmp_path):
        f = self._write(
            tmp_path,
            "j.jsonl",
            json.dumps(_MPF_RECORD)
            + "\n"
            + json.dumps(_SIDECAR_TRAILER)
            + "\n"
            + json.dumps({"content": "flat two"})
            + "\n",
        )
        importer = JsonImporter(str(f), preserve_metadata=True, dry_run=True)
        stats, _ = _dry(importer.run)
        assert stats == {"imported": 2, "failed": 0, "skipped": 0}
        assert isinstance(importer.source_envelope, dict)
        assert importer.source_envelope["kg_triples"] == _SIDECAR_TRAILER["kg_triples"]
        kinds = [r.get("kind") for r in importer.source_envelope["records"]]
        assert kinds == ["memory", "memory"]  # mpf record + lifted flat line

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "FB73 finding FB73-FINDING-1: a JSONL export with a sidecar trailer "
            "imported without --preserve-metadata only prints "
            "'sidecars will NOT be imported' and then still reports a fully "
            "successful import (failed=0) while silently dropping the sidecars. "
            "A data-losing import must not read as success; flip to a real test "
            "when the exit accounting reflects the dropped sidecars."
        ),
    )
    def test_xfail_sidecar_trailer_without_preserve_metadata_is_not_reported_as_success(self, tmp_path, capsys):
        f = self._write(
            tmp_path,
            "k.jsonl",
            json.dumps(_MPF_RECORD)
            + "\n"
            + json.dumps(_SIDECAR_TRAILER)
            + "\n",
        )
        stats, _ = _dry(JsonImporter(str(f), dry_run=True).run)
        assert stats["failed"] > 0  # today: failed == 0 while the sidecars are dropped
        assert "sidecars will NOT be imported" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# mnemos.portability.charon — bundle export / import
# ---------------------------------------------------------------------------


class TestCharonBundle:
    def test_manifest_shape_and_layout(self, tmp_path):
        manifest = charon.export_bundle([dict(_CONFORMANT_MEMORY)], tmp_path)
        assert manifest["mif_version"] == "1.0.0"
        assert manifest["generator"] == "mnemos-core"
        assert manifest["count"] == 1
        assert manifest["context"]
        assert manifest["schema"]
        entries = manifest["concepts"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["@id"].startswith("urn:mif:")
        ctype_dir, md_name = entry["path"].split("/")
        assert ctype_dir == "semantic"
        assert md_name.endswith(".md")
        assert (tmp_path / ctype_dir).is_dir()
        assert entry["mnemos_id"] == _CONFORMANT_MEMORY["id"]
        md_files = list(tmp_path.rglob("*.md"))
        assert len(md_files) == 1

    def test_round_trip_recovers_the_memory(self, tmp_path):
        src = [dict(_CONFORMANT_MEMORY)]
        charon.export_bundle(src, tmp_path)
        out = charon.import_bundle(tmp_path)
        assert len(out) == 1
        row = out[0]
        assert row["id"] == src[0]["id"]
        assert row["content"] == src[0]["content"]
        assert row["category"] == src[0]["category"]

    def test_non_conformant_memory_raises_a_value_error_naming_the_memory(self, tmp_path):
        bad = dict(_CONFORMANT_MEMORY)
        bad["id"] = "mem_bad"
        bad["content"] = ""
        with pytest.raises(ValueError) as excinfo:
            charon.export_bundle([bad], tmp_path / "bad")
        message = str(excinfo.value)
        assert "'mem_bad'" in message
        assert "non-conformant MIF concept" in message
        assert "content" in message

    def test_validate_false_skips_the_conformance_check(self, tmp_path):
        bad = dict(_CONFORMANT_MEMORY)
        bad["id"] = "mem_bad"
        bad["content"] = ""
        manifest = charon.export_bundle([bad], tmp_path / "bad2", validate=False)
        assert manifest["count"] == 1

    def test_stream_manifest_keeps_the_return_shape_and_writes_full_disk_manifest(self, tmp_path):
        manifest = charon.export_bundle([dict(_CONFORMANT_MEMORY)], tmp_path, stream_manifest=True)
        assert set(manifest) == {"mif_version", "schema", "context", "generator", "count"}
        disk = json.loads((tmp_path / charon.MANIFEST_NAME).read_text(encoding="utf-8"))
        assert disk["count"] == 1
        assert len(disk["concepts"]) == 1
        assert (tmp_path / disk["concepts"][0]["path"]).is_file()

    def test_import_of_an_empty_directory_returns_no_rows(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert charon.import_bundle(empty) == []


class TestCharonRowAdapters:
    def test_row_to_memory_dict_coerces_json_text_and_embedding(self, tmp_path):
        class Row(dict):
            def keys(self):  # mimics the aiosqlite row interface
                return iter(("metadata", "merge_parents", "embedding", "other"))

        row = Row(metadata='{"a": 1}', merge_parents='["x"]', embedding="[1, 2]", other=5)
        out = charon._row_to_memory_dict(row)
        assert out == {"metadata": {"a": 1}, "merge_parents": ["x"], "embedding": [1.0, 2.0], "other": 5}

    def test_sidecar_id_insertion_rules(self):
        def _new():
            return "NEW"

        assert charon._sidecar_id_for_insert("mem_1", preserve_ids=True, requires_uuid=False, new_id=_new) == "mem_1"
        assert charon._sidecar_id_for_insert("mem_1", preserve_ids=True, requires_uuid=True, new_id=_new) == "NEW"
        assert charon._sidecar_id_for_insert(str(uuid.uuid4()), preserve_ids=True, requires_uuid=True, new_id=_new) != "NEW"
        assert charon._sidecar_id_for_insert("mem_1", preserve_ids=False, requires_uuid=False, new_id=_new) == "NEW"

    def test_new_memory_id_shape(self):
        value = charon._new_memory_id()
        prefix, _us, tail = value.split("_")
        assert prefix == "mem"
        assert len(tail) == 6
        assert all(c in "0123456789abcdef" for c in tail)

    def test_new_memory_ids_are_unique(self):
        ids = {charon._new_memory_id() for _ in range(50)}
        assert len(ids) == 50
