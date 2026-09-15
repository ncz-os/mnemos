"""Durable, content-free federation change tracking.

Database triggers append an identity-keyed outbox without locking the publisher
clock. A short feed transaction assigns sequences only to committed outbox rows.
The cursor orders published changes, while each payload represents the latest
authorized state. A scope change therefore revokes an old subscriber and gives
a still-authorized subscriber the current row, using the same source version.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from mnemos.core.config import federation_feed_include_private
from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect

CURSOR_PREFIX = "journal:"


class FeedRows(list):
    """List-compatible page with a content-free publisher checkpoint."""

    def __init__(self, rows, *, checkpoint, pending):
        super().__init__(rows)
        self.checkpoint = checkpoint
        self.pending = pending


def _ops(tx: Any) -> _Ops:
    return _Ops(tx, transaction_dialect(tx))


def supports_journal(tx: Any) -> bool:
    """Legacy asyncpg-shaped test/extension adapters retain their old contract."""
    try:
        transaction_dialect(tx)
    except TypeError:
        return False
    return True


async def prepare_versioned_update(tx, peer_name, remote_id):
    """The durable sequence fence supersedes the legacy timestamp predicate.

    Clear the legacy marker only inside the fenced mutation transaction; the
    existing repository then writes the actual remote timestamp with the data.
    This handles source clocks moving backwards and changes sharing a timestamp.
    """
    await _ops(tx).execute(
        "UPDATE memories SET federation_remote_updated = NULL, deleted_at = NULL, "
        "consolidated_into = NULL, consolidated_at = NULL WHERE id = ? AND federation_source = ?",
        f"fed:{peer_name}:{remote_id}",
        peer_name,
    )


def _scope_clause(namespaces, categories, *, alias: str = "e"):
    branches, params = [], []
    for side in ("old", "new"):
        prefix = f"{alias}.{side}"
        parts = [f"{prefix}_exportable = 1"]
        if not federation_feed_include_private():
            parts.append(f"{prefix}_public = 1")
        for column, values in (("namespace", namespaces), ("category", categories)):
            if values:
                parts.append(f"{prefix}_{column} IN ({','.join('?' for _ in values)})")
                params.extend(values)
        branches.append("(" + " AND ".join(parts) + ")")
    return "(" + " OR ".join(branches) + ")", params


def _limit(ops: _Ops, limit: int) -> str:
    value = max(1, min(int(limit), 10001))
    return f" FETCH FIRST {value} ROWS ONLY" if ops.dialect in {"oracle", "db2"} else f" LIMIT {value}"


async def _publish(ops: _Ops, memory_id=None):
    """Assign cursor positions after commit, without blocking source writers.

    An identity allocated by an uncommitted mutation may be lower than an
    already published ID. That is harmless: it gets a higher published sequence
    on a later poll, so a consumer never advances past an invisible event.
    """
    await ops.execute("UPDATE federation_change_clock SET value = value WHERE id = 1")
    pending = await ops.fetchall(
        "SELECT event_id FROM federation_changes WHERE seq IS NULL"
        + (" AND memory_id = ?" if memory_id is not None else "")
        + " ORDER BY event_id"
        + _limit(ops, 256),
        *((memory_id,) if memory_id is not None else ()),
    )
    if not pending:
        return
    value = int(await ops.scalar("SELECT value FROM federation_change_clock WHERE id = 1"))
    ids = [int(row["event_id"]) for row in pending]
    cases = " ".join(f"WHEN ? THEN {index}" for index, _ in enumerate(ids, 1))
    integer_type = {"oracle": "NUMBER(19)", "mysql": "SIGNED"}.get(ops.dialect, "BIGINT")
    await ops.execute(
        f"UPDATE federation_changes SET seq = CAST(? AS {integer_type}) + CASE event_id {cases} END "
        f"WHERE event_id IN ({','.join('?' for _ in ids)}) AND seq IS NULL",
        value,
        *ids,
        *ids,
    )
    await ops.execute("UPDATE federation_change_clock SET value = ? WHERE id = 1", value + len(ids))


async def _current_states(ops, ids, prefer_compressed=False, *, repo=None, include_embedding=False):
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    # One statement snapshot binds the row to its latest source sequence, even
    # on READ COMMITTED. Reading these separately can label new data with an
    # older sequence (or old data with a newer one) during concurrent writes.
    compressed_column = ", v.compressed_content AS journal_compressed" if prefer_compressed else ""
    compressed_join = " LEFT JOIN memory_compressed_variants v ON v.memory_id = m.id" if prefer_compressed else ""
    embedding_column = ", m.embedding" if include_embedding else ""
    embedding_join = ""
    if include_embedding and getattr(repo, "_journal_embedding_sql", None):
        embedding_column = ", " + repo._journal_embedding_sql + " AS journal_embedding_value"
        embedding_join = repo._journal_embedding_join
    # Do not fetch multi-kilobyte vectors when the peer did not request them.
    memory_columns = ", ".join(
        "m." + name
        for name in (
            "id",
            "content",
            "category",
            "subcategory",
            "metadata",
            "quality_rating",
            "verbatim_content",
            "source_model",
            "source_provider",
            "source_session",
            "source_agent",
            "owner_id",
            "namespace",
            "permission_mode",
            "federation_source",
            "deleted_at",
            "archived_at",
            "consolidated_into",
            "consolidated_at",
            "created",
            "updated",
        )
    )
    rows = await ops.fetchall(
        "SELECT e.memory_id AS journal_memory_id, e.seq AS journal_sequence, "
        "e.changed_at AS journal_updated, "
        + memory_columns
        + compressed_column
        + embedding_column
        + " FROM federation_changes e "
        "JOIN (SELECT memory_id, MAX(seq) AS latest_seq FROM federation_changes "
        f"WHERE memory_id IN ({placeholders}) GROUP BY memory_id) latest "
        "ON e.seq = latest.latest_seq LEFT JOIN memories m ON m.id = e.memory_id" + compressed_join + embedding_join,
        *ids,
    )
    return {row["journal_memory_id"]: row for row in rows}


def _authorized(row, namespaces, categories):
    return (
        row.get("id") is not None
        and row.get("federation_source") is None
        and row.get("namespace") != "vault"
        and row.get("deleted_at") is None
        and row.get("archived_at") is None
        and row.get("consolidated_into") is None
        and (federation_feed_include_private() or int(row.get("permission_mode") or 0) % 10 >= 4)
        and (not namespaces or row.get("namespace") in namespaces)
        and (not categories or row.get("category") in categories)
    )


async def _resolve(event, current, namespaces, categories, include_embedding=False):
    if _authorized(current, namespaces, categories):
        result = dict(current)
        if "journal_embedding_value" in result:
            result["embedding"] = result.pop("journal_embedding_value")
        # Oracle LOBs must be consumed before the connection leaves its tx.
        from mnemos.persistence.worker_lifecycle import _await

        for name in ("content", "verbatim_content", "metadata", "embedding", "journal_compressed"):
            value = result.get(name)
            if hasattr(value, "read"):
                result[name] = await _await(value.read())
        result["type"] = None
        result["compressed_content"] = None
        compressed = result.get("journal_compressed")

        def wire_size(value):
            return len(json.dumps(value, ensure_ascii=False).encode("utf-8")) if value is not None else 0

        if compressed and 2 * wire_size(compressed) < wire_size(result["content"]) + wire_size(
            result.get("verbatim_content")
        ):
            result["content"] = compressed
            result["compressed_content"] = compressed
            result["verbatim_content"] = None
        if include_embedding:
            from mnemos.core.config import embed_http_model_override, get_settings

            result["embedding_model"] = (
                embed_http_model_override() or get_settings().providers.inference_embed_model or "unknown"
            )
        else:
            result.pop("embedding", None)
    elif (
        current.get("consolidated_into")
        and current.get("federation_source") is None
        and current.get("namespace") != "vault"
        and current.get("deleted_at") is None
        and current.get("archived_at") is None
        and (federation_feed_include_private() or int(current.get("permission_mode") or 0) % 10 >= 4)
        and (not namespaces or current.get("namespace") in namespaces)
        and (not categories or current.get("category") in categories)
    ):
        result = {
            "id": event["memory_id"],
            "type": "consolidation",
            "consolidated_into": current["consolidated_into"],
            "consolidated_at": current.get("consolidated_at") or current["journal_updated"],
            "updated": current["journal_updated"],
            "created": current["journal_updated"],
        }
    else:
        result = {
            "id": event["memory_id"],
            "type": "withdrawal",
            "namespace": event.get("old_namespace") or event.get("new_namespace"),
            "updated": current["journal_updated"],
            "created": current["journal_updated"],
        }
    result["federation_sequence"] = int(current["journal_sequence"])
    result["cursor_id"] = f"{CURSOR_PREFIX}{event['seq']}"
    result["cursor_updated"] = event["changed_at"]
    return result


async def feed_query(
    repo, tx, *, since_updated, since_id, namespaces, categories, limit, prefer_compressed, include_embedding=False
):
    ops = _ops(tx)
    await _publish(ops)
    sequence = 0
    if isinstance(since_id, str) and since_id.startswith(CURSOR_PREFIX):
        raw = since_id[len(CURSOR_PREFIX) :]
        if not raw.isdecimal():
            raise ValueError("invalid federation journal cursor")
        sequence = int(raw)
    # A legacy timestamp cannot locate a sequence reliably, especially across
    # migration. Replaying current authorized state is safe and lossless.
    scope, params = _scope_clause(namespaces, categories)
    events = await ops.fetchall(
        "SELECT e.* FROM federation_changes e WHERE e.seq > ? AND " + scope + " ORDER BY e.seq" + _limit(ops, limit),
        sequence,
        *params,
    )
    current = await _current_states(
        ops,
        list(dict.fromkeys(e["memory_id"] for e in events)),
        prefer_compressed,
        repo=repo,
        include_embedding=include_embedding,
    )
    rows = [
        await _resolve(event, current[event["memory_id"]], namespaces, categories, include_embedding)
        for event in events
    ]
    checkpoint = int(await ops.scalar("SELECT value FROM federation_change_clock WHERE id = 1"))
    pending = await ops.fetchone("SELECT event_id FROM federation_changes WHERE seq IS NULL" + _limit(ops, 1))
    return FeedRows(rows, checkpoint=checkpoint, pending=pending is not None)


async def get_feed_memory(repo, tx, memory_id, *, namespaces, categories):
    ops = _ops(tx)
    await _publish(ops, memory_id)
    scope, params = _scope_clause(namespaces, categories)
    event = await ops.fetchone(
        "SELECT e.* FROM federation_changes e WHERE e.memory_id = ? AND e.seq IS NOT NULL AND "
        + scope
        + " ORDER BY e.seq DESC"
        + _limit(ops, 1),
        memory_id,
        *params,
    )
    if event is None:
        return None
    current = await _current_states(ops, [memory_id])
    return await _resolve(event, current[memory_id], namespaces, categories)


def _datetime(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return None


async def accept_event(tx, peer_name, remote_id, *, sequence, remote_updated, withdrawn):
    """Fence replay and persist deletion knowledge in the mutation transaction."""
    ops = _ops(tx)
    # Also serializes the first insert, for which a SELECT FOR UPDATE has no
    # row to lock. This lock shares the source clock but does not increment it.
    await ops.execute("UPDATE federation_change_clock SET value = value WHERE id = 1")
    state = await ops.fetchone(
        "SELECT last_sequence, remote_updated, withdrawn FROM federation_receive_state "
        "WHERE peer_name = ? AND remote_id = ?",
        peer_name,
        remote_id,
    )
    timestamp = _datetime(remote_updated)
    if sequence is not None:
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValueError("invalid federation source sequence")
        if state and state["last_sequence"] is not None and sequence <= int(state["last_sequence"]):
            return False
    elif state:
        if state["last_sequence"] is not None:
            return False
        prior = _datetime(state["remote_updated"])
        if prior is not None and (timestamp is None or timestamp <= prior):
            return False
    if state:
        await ops.execute(
            "UPDATE federation_receive_state SET last_sequence = ?, remote_updated = ?, withdrawn = ? "
            "WHERE peer_name = ? AND remote_id = ?",
            sequence,
            timestamp,
            int(withdrawn),
            peer_name,
            remote_id,
        )
    else:
        await ops.execute(
            "INSERT INTO federation_receive_state(peer_name, remote_id, last_sequence, remote_updated, withdrawn) "
            "VALUES (?, ?, ?, ?, ?)",
            peer_name,
            remote_id,
            sequence,
            timestamp,
            int(withdrawn),
        )
    return True


async def load_peer_cursor(tx, peer_id, filter_signature):
    row = await _ops(tx).fetchone(
        "SELECT cursor_value FROM federation_peer_cursors WHERE peer_id = ? AND filter_signature = ?",
        peer_id,
        filter_signature,
    )
    return row["cursor_value"] if row else None


async def save_peer_cursor(tx, peer_id, cursor_value, filter_signature):
    ops = _ops(tx)
    await ops.execute("UPDATE federation_change_clock SET value = value WHERE id = 1")
    row = await ops.fetchone("SELECT peer_id FROM federation_peer_cursors WHERE peer_id = ?", peer_id)
    if row:
        await ops.execute(
            "UPDATE federation_peer_cursors SET cursor_value = ?, filter_signature = ? WHERE peer_id = ?",
            cursor_value,
            filter_signature,
            peer_id,
        )
    else:
        await ops.execute(
            "INSERT INTO federation_peer_cursors(peer_id,cursor_value,filter_signature) VALUES (?,?,?)",
            peer_id,
            cursor_value,
            filter_signature,
        )
