"""Unit tests for v6.2 M-2.2.1 audit chain entry builder."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from mnemos.audit import build_entry, latest_hash, verify_entry


SS = b"x" * 32  # session_secret


class TestBuildEntry:
    def test_first_entry_for_memory(self) -> None:
        mid = uuid.uuid4().bytes
        ph = b"\x11" * 32
        entry, sig = build_entry(
            op="create",
            memory_id=mid,
            prev_entry_id=None,
            prev_entry_hash=None,
            payload_hash=ph,
            writer_id="alice",
            session_secret=SS,
        )
        assert entry.op == "create"
        assert entry.memory_id == mid
        assert entry.prev_entry_id is None
        assert entry.prev_entry_hash is None
        assert entry.payload_hash == ph
        assert entry.writer_id == "alice"
        assert len(entry.writer_pubkey) == 32
        assert len(sig) == 64
        assert verify_entry(entry, sig) is True

    def test_chains_to_prev(self) -> None:
        mid = uuid.uuid4().bytes
        # entry 1
        e1, s1 = build_entry(
            op="create",
            memory_id=mid,
            prev_entry_id=None,
            prev_entry_hash=None,
            payload_hash=b"\x01" * 32,
            writer_id="alice",
            session_secret=SS,
        )
        h1 = latest_hash(e1, s1)
        # entry 2 chains to entry 1
        e2, s2 = build_entry(
            op="update",
            memory_id=mid,
            prev_entry_id=e1.entry_id,
            prev_entry_hash=h1,
            payload_hash=b"\x02" * 32,
            writer_id="alice",
            session_secret=SS,
        )
        assert e2.prev_entry_id == e1.entry_id
        assert e2.prev_entry_hash == h1
        assert verify_entry(e2, s2) is True

    def test_writer_keypair_deterministic_across_calls(self) -> None:
        """Same writer_id under same session_secret -> same pubkey
        across separate builder invocations."""
        mid = uuid.uuid4().bytes
        e1, _ = build_entry(
            op="create",
            memory_id=mid,
            prev_entry_id=None,
            prev_entry_hash=None,
            payload_hash=b"\x00" * 32,
            writer_id="alice",
            session_secret=SS,
        )
        e2, _ = build_entry(
            op="create",
            memory_id=uuid.uuid4().bytes,
            prev_entry_id=None,
            prev_entry_hash=None,
            payload_hash=b"\x00" * 32,
            writer_id="alice",
            session_secret=SS,
        )
        assert e1.writer_pubkey == e2.writer_pubkey

    def test_different_writers_different_pubkeys(self) -> None:
        mid = uuid.uuid4().bytes
        e_a, _ = build_entry(
            op="create",
            memory_id=mid,
            prev_entry_id=None,
            prev_entry_hash=None,
            payload_hash=b"\x00" * 32,
            writer_id="alice",
            session_secret=SS,
        )
        e_b, _ = build_entry(
            op="create",
            memory_id=mid,
            prev_entry_id=None,
            prev_entry_hash=None,
            payload_hash=b"\x00" * 32,
            writer_id="bob",
            session_secret=SS,
        )
        assert e_a.writer_pubkey != e_b.writer_pubkey

    def test_explicit_signed_at(self) -> None:
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        e, _ = build_entry(
            op="create",
            memory_id=uuid.uuid4().bytes,
            prev_entry_id=None,
            prev_entry_hash=None,
            payload_hash=b"\x00" * 32,
            writer_id="alice",
            session_secret=SS,
            signed_at=ts,
        )
        assert e.signed_at == ts.isoformat()


class TestBuildEntryValidation:
    def test_short_memory_id(self) -> None:
        with pytest.raises(ValueError, match="memory_id must be 16"):
            build_entry(
                op="create",
                memory_id=b"\x00" * 8,
                prev_entry_id=None,
                prev_entry_hash=None,
                payload_hash=b"\x00" * 32,
                writer_id="alice",
                session_secret=SS,
            )

    def test_short_payload_hash(self) -> None:
        with pytest.raises(ValueError, match="payload_hash must be 32"):
            build_entry(
                op="create",
                memory_id=uuid.uuid4().bytes,
                prev_entry_id=None,
                prev_entry_hash=None,
                payload_hash=b"\x00" * 16,
                writer_id="alice",
                session_secret=SS,
            )

    def test_prev_half_state_rejected_id_only(self) -> None:
        """prev_entry_id without prev_entry_hash."""
        with pytest.raises(ValueError, match="both be set or both be None"):
            build_entry(
                op="update",
                memory_id=uuid.uuid4().bytes,
                prev_entry_id=uuid.uuid4().bytes,
                prev_entry_hash=None,
                payload_hash=b"\x00" * 32,
                writer_id="alice",
                session_secret=SS,
            )

    def test_prev_half_state_rejected_hash_only(self) -> None:
        with pytest.raises(ValueError, match="both be set or both be None"):
            build_entry(
                op="update",
                memory_id=uuid.uuid4().bytes,
                prev_entry_id=None,
                prev_entry_hash=b"\x00" * 32,
                payload_hash=b"\x00" * 32,
                writer_id="alice",
                session_secret=SS,
            )

    def test_short_prev_entry_id(self) -> None:
        with pytest.raises(ValueError, match="prev_entry_id must be 16"):
            build_entry(
                op="update",
                memory_id=uuid.uuid4().bytes,
                prev_entry_id=b"\x00" * 8,
                prev_entry_hash=b"\x00" * 32,
                payload_hash=b"\x00" * 32,
                writer_id="alice",
                session_secret=SS,
            )

    def test_short_prev_entry_hash(self) -> None:
        with pytest.raises(ValueError, match="prev_entry_hash must be 32"):
            build_entry(
                op="update",
                memory_id=uuid.uuid4().bytes,
                prev_entry_id=uuid.uuid4().bytes,
                prev_entry_hash=b"\x00" * 16,
                payload_hash=b"\x00" * 32,
                writer_id="alice",
                session_secret=SS,
            )


class TestLatestHash:
    def test_changes_with_signature(self) -> None:
        e1, s1 = build_entry(
            op="create",
            memory_id=uuid.uuid4().bytes,
            prev_entry_id=None,
            prev_entry_hash=None,
            payload_hash=b"\x00" * 32,
            writer_id="alice",
            session_secret=SS,
        )
        h1 = latest_hash(e1, s1)
        # Different signature on same entry → different hash
        h_alt = latest_hash(e1, b"\xff" * 64)
        assert h1 != h_alt

    def test_chain_three_entries(self) -> None:
        """Build 3 entries chained, then independently verify every
        signature + every prev_entry_hash linkage."""
        mid = uuid.uuid4().bytes
        prev_id: bytes | None = None
        prev_h: bytes | None = None
        entries = []
        for i, op in enumerate(["create", "update", "update"]):
            ts = datetime(2026, 1, 1, 0, 0, i, tzinfo=timezone.utc)
            e, s = build_entry(
                op=op,
                memory_id=mid,
                prev_entry_id=prev_id,
                prev_entry_hash=prev_h,
                payload_hash=bytes([i + 1]) * 32,
                writer_id="alice",
                session_secret=SS,
                signed_at=ts,
            )
            entries.append((e, s))
            prev_id = e.entry_id
            prev_h = latest_hash(e, s)
        # All three verify
        for e, s in entries:
            assert verify_entry(e, s) is True
        # Linkage: e2.prev == hash(e1), e3.prev == hash(e2)
        h1 = latest_hash(*entries[0])
        h2 = latest_hash(*entries[1])
        assert entries[1][0].prev_entry_hash == h1
        assert entries[2][0].prev_entry_hash == h2
