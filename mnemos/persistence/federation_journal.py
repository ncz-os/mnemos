"""Durable, content-free federation change tracking.

Database triggers append an identity-keyed outbox without locking the publisher
clock. A short feed transaction assigns sequences only to committed outbox rows.
The cursor orders published changes, while each payload represents the latest
authorized state. A scope change therefore revokes an old subscriber and gives
a still-authorized subscriber the current row, using the same source version.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any

from mnemos.core.config import federation_feed_include_private
from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect

CURSOR_PREFIX = "journal:"
# Rows sequenced per ``_publish`` call; bounds the CASE/IN statement size.
_PUBLISH_BATCH = 256


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

    ``archived_at`` is cleared for the same reason as ``deleted_at``: a local
    sweep that archived a replica replaced the body with an ``ARCHIVED:<id>``
    marker, so without this the fresh source content lands on a row that stays
    hidden as a stub, and a later restore writes the stale payload back over it.
    """
    await _ops(tx).execute(
        "UPDATE memories SET federation_remote_updated = NULL, deleted_at = NULL, "
        "consolidated_into = NULL, consolidated_at = NULL, archived_at = NULL "
        "WHERE id = ? AND federation_source = ?",
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


async def _publish(ops: _Ops, memory_id=None, *, memory_ids=None):
    """Assign cursor positions after commit, without blocking source writers.

    An identity allocated by an uncommitted mutation may be lower than an
    already published ID. That is harmless: it gets a higher published sequence
    on a later poll, so a consumer never advances past an invisible event.

    ``memory_ids`` narrows the sweep to a known set of rows. Redispatch uses it
    so it publishes *its own* events and never claims a concurrent publisher's
    pending outbox row -- a global sweep would report those as re-advertised.

    Returns how many rows this call sequenced: at most one ``_PUBLISH_BATCH``.
    """
    await ops.execute("UPDATE federation_change_clock SET value = value WHERE id = 1")
    where = ""
    params: list[Any] = []
    if memory_ids is not None:
        ids_in = [str(mid) for mid in memory_ids]
        if not ids_in:
            return 0
        where = " AND memory_id IN (" + ",".join("?" for _ in ids_in) + ")"
        params = ids_in
    elif memory_id is not None:
        where = " AND memory_id = ?"
        params = [memory_id]
    pending = await ops.fetchall(
        "SELECT event_id FROM federation_changes WHERE seq IS NULL"
        + where
        + " ORDER BY event_id"
        + _limit(ops, _PUBLISH_BATCH),
        *params,
    )
    if not pending:
        return 0
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
    return len(ids)


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
        # M2: matched on the memory too. ``seq`` is assigned from one clock, so a
        # duplicated value means the journal diverged -- joining on it alone then
        # cross-multiplies the latest row of *every* memory into each event.
        "ON e.seq = latest.latest_seq AND e.memory_id = latest.memory_id "
        "LEFT JOIN memories m ON m.id = e.memory_id" + compressed_join + embedding_join,
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


# ── F-3: the local-embed queue ─────────────────────────────────────────────
#
# A pulled row lands with ``embedding IS NULL`` unless the peer copied a vector
# this node could verify. Those rows are invisible to semantic search until
# something embeds them, so they are the durable work queue of the local
# embedder (mnemos.domain.federation.local_embed).
#
# The queue is the PREDICATE, not a side table: nothing is held in memory
# between passes, so a restart resumes and no row can be lost between the sync
# and the worker.
#
# The vector lives in a different place per dialect -- an inline
# ``memories.embedding`` column on Postgres/Oracle/Db2/MySQL, a
# ``memory_embeddings`` join table on SQLite/MariaDB -- so the "has no
# vector" test is per-dialect and lives on each backend's repository.
#
# Two dialect facts have to travel WITH the query, not be guessed from it:
#
# * the SHAPE (inline column vs. join table) -- pushed in as ``join_table``;
# * the PLACEHOLDER STYLE -- qmark (``?``) for SQLite/Db2, percent (``%s``)
#   for MySQL/MariaDB, dollar (``$N``) for Postgres, named (``:name``) for
#   Oracle.
#
# The placeholder style was the defect this helper used to have: it hardcoded
# the qmark form while accepting only a boolean dialect hint, so every caller
# that routes through it bound a qmark against a driver that wants ``%s`` or
# ``:n``. The style is now resolved from the transaction's dialect by
# ``_Ops.sql``, exactly like every other shared lifecycle query.

#: The inline-``memories.embedding`` predicate (Postgres/Oracle/Db2/MySQL).
_MISSING_EMBEDDING_INLINE = (
    "federation_source IS NOT NULL AND embedding IS NULL "
    "AND deleted_at IS NULL AND archived_at IS NULL AND consolidated_into IS NULL"
)

#: The ``memory_embeddings`` join-table predicate (SQLite/MariaDB).
_MISSING_EMBEDDING_JOIN = (
    "m.federation_source IS NOT NULL "
    "AND m.deleted_at IS NULL "
    "AND m.archived_at IS NULL "
    "AND m.consolidated_into IS NULL "
    "AND me.embedding IS NULL"
)


def missing_embedding_predicate(*, join_table: bool) -> str:
    """The "pulled row with no vector" predicate for one dialect shape.

    ``join_table=True`` for the backends that keep the vector in the
    ``memory_embeddings`` table (SQLite, MariaDB); ``False`` for the
    backends with an inline ``memories.embedding`` column (Postgres, Oracle,
    Db2, MySQL).
    """
    return _MISSING_EMBEDDING_JOIN if join_table else _MISSING_EMBEDDING_INLINE


async def count_federated_missing_embeddings(tx, *, peer_name=None, join_table: bool) -> int:
    """Depth of the local-embed queue, optionally for one peer.

    The query is written with the qmark placeholder and handed to ``_Ops``,
    which rewrites the marker to the transaction's dialect (``%s`` for
    MySQL/MariaDB, ``$N`` for Postgres, ``:n`` for Oracle, ``?`` for
    SQLite/Db2). The peer filter is bound as a PARAMETER, never
    interpolated, so a peer name can never become SQL.
    """
    ops = _ops(tx)
    predicate = missing_embedding_predicate(join_table=join_table)
    if join_table:
        sql = (
            "SELECT COUNT(*) AS n FROM memories m "
            "LEFT JOIN memory_embeddings me ON me.memory_id = m.id "
            f"WHERE {predicate}"
        )
        peer_column = "m.federation_source"
    else:
        sql = f"SELECT COUNT(*) AS n FROM memories WHERE {predicate}"
        peer_column = "federation_source"
    params: list[Any] = []
    if peer_name is not None:
        sql += f" AND {peer_column} = ?"
        params.append(peer_name)
    row = await ops.fetchone(sql, *params)
    return int(next(iter(row.values()))) if row else 0


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


def _cutoff(days: int) -> datetime:
    """The retention cutoff, as a tz-aware UTC ``datetime``.

    Never a pre-formatted string: ``_Ops.sql`` only rewrites ``?`` to ``$n``
    for Postgres, and the column type differs per backend (TEXT on SQLite,
    TIMESTAMPTZ on Postgres) -- the ``_Ops`` wrapper decides how a
    ``datetime`` is bound for each dialect.
    """
    return datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))


def compaction_sql(dialect: str) -> str:
    """``DELETE`` superseded journal events; one dialect-valid statement each.

    Two shapes, because the statement cannot be written once:

    * MySQL/MariaDB forbid a correlated subquery that references the table
      being deleted from (error 1093), so the newest event per memory is
      materialised by a derived table and the delete joins ``NOT IN``.
    * Oracle has no ``DELETE FROM t WHERE ...`` over a joined target unless
      the target is aliased, and Db2 rejects the unaliased form for the same
      statement, so those two use the ``stale`` alias one level up from the
      correlation.

    Everything else -- SQLite and Postgres -- takes the plain correlated
    ``EXISTS``, which is also the shape the SQLite tests exercise.
    """
    if dialect == "mysql":
        return (
            "DELETE FROM federation_changes WHERE seq IS NOT NULL AND changed_at < ? AND seq NOT IN ("
            "  SELECT keep_seq FROM ("
            "    SELECT MAX(seq) AS keep_seq FROM federation_changes "
            "    WHERE seq IS NOT NULL AND changed_at < ? "
            "    GROUP BY memory_id"
            "  ) AS keep"
            ")"
        )
    if dialect in {"oracle", "db2"}:
        return (
            "DELETE FROM federation_changes stale "
            "WHERE stale.seq IS NOT NULL "
            "AND stale.changed_at < ? "
            "AND EXISTS ("
            "  SELECT 1 FROM federation_changes newer "
            "  WHERE newer.memory_id = stale.memory_id "
            "  AND newer.seq IS NOT NULL "
            "  AND newer.seq > stale.seq "
            # A successor only retires a row if it is itself compactable.
            "  AND newer.changed_at < ?"
            ")"
        )
    return (
        "DELETE FROM federation_changes "
        "WHERE seq IS NOT NULL "
        "AND changed_at < ? "
        "AND EXISTS ("
        "  SELECT 1 FROM federation_changes AS newer "
        "  WHERE newer.memory_id = federation_changes.memory_id "
        "  AND newer.seq IS NOT NULL "
        "  AND newer.seq > federation_changes.seq "
        # Only a *compactable* successor retires a row. Without this the
        # newest published event can be dropped when a later, still-recent
        # event exists (it is inside a consumer's in-flight page, so it is
        # not itself a keeper), which would hide the memory from the feed.
        "  AND newer.changed_at < ?"
        ")"
    )


async def compact(tx: Any, *, older_than_days: int = 30) -> int:
    """F-4: drop superseded journal events, keeping one per memory.

    The feed resolves each memory through its newest published event, so
    every event except that one contributes nothing to any future consumer
    and has already been acknowledged by every cursor past it. This deletes
    those.

    Two guards make it safe to run on a live source:

    * ``seq IS NOT NULL`` -- an unpublished event has not been handed to a
      consumer yet; ``_publish`` owns draining that backlog and compaction
      must never race it. Only *published* events are candidates, so the
      survivor is the newest published event, which is the one the feed
      already resolves to.
    * ``changed_at < cutoff`` -- a recent event may still be inside a
      consumer's in-flight page.

    The newest event is kept *whatever its age*: dropping it would make the
    memory invisible to a consumer that has not caught up, and a
    re-appearing row is worse than one extra row per memory.

    Ordered by ``seq``, never by ``event_id``/``changed_at``: the two are
    independent (``seq`` is assigned at publish time, events can publish out
    of insertion order), so "the newest published event" is the ``MAX(seq)``
    the feed itself uses -- see :func:`compaction_sql`.

    Returns the number of events removed. ``0`` when
    ``MNEMOS_FEDERATION_JOURNAL_COMPACTION`` is off.
    """
    from mnemos.core.config import federation_journal_compaction_enabled

    if not federation_journal_compaction_enabled():
        return 0
    ops = _ops(tx)
    cutoff = _cutoff(older_than_days)
    sql = compaction_sql(ops.dialect)
    # Every dialect's statement now takes the cutoff twice: the outer
    # candidate filter and the "is there a compactable successor" test.
    return int(await ops.execute(sql, cutoff, cutoff))


def _redispatch_eligible_predicate() -> str:
    """The feed's own export predicate, as an unaliased ``memories`` filter.

    ``redispatch_memory_ids`` re-advertises CURRENT STATE, so the only rows it
    may publish are the ones the live feed itself would serve. Reusing
    ``eligible_for_federation`` is what keeps the two definitions from
    drifting: it already carries the ``federation_source IS NULL`` loop-guard,
    the lifecycle exclusions, the vault credential boundary and the world-read
    gate. Hand-writing a subset of those terms (the round-1 defect) let a vault
    row through and published a revocation as live content.
    """
    from mnemos.core.eligibility import eligible_for_federation

    return eligible_for_federation("")


async def redispatch_memory_ids(tx, memory_ids):
    """Append a fresh, publishable change for each id that needs re-serving.

    F-15 reconcile: the receiver's durable fence drops any event whose
    sequence it already applied, so a replica that lost a row locally can
    never re-apply the original one. Publishing a NEW event for the current
    state -- stamped in the source's clock order by the next ``_publish`` --
    is how the row becomes reachable again without touching the fence, the
    receiver, or the diagnostic history.

    Only the current state of an exportable row is re-advertised: a row that
    left the feed (deleted, archived, consolidated, vault, or became private
    offsite) already produces its own withdrawal/consolidation event, and
    re-advertising it would mislabel a revocation as live data. Rows with
    ``federation_source`` set are skipped for the same reason they are never
    on the feed: they have no publishable event to replay.
    """
    ids = [str(mid) for mid in memory_ids if mid]
    if not ids:
        return 0
    ops = _ops(tx)
    placeholders = ",".join("?" for _ in ids)
    rows = await ops.fetchall(
        "SELECT id, namespace, category, permission_mode, updated FROM memories "
        f"WHERE id IN ({placeholders}) AND " + _redispatch_eligible_predicate(),
        *ids,
    )
    if not rows:
        # Nothing to publish: no clock touch, no write lock on the journal.
        return 0
    for row in rows:
        public = 1 if (federation_feed_include_private() or int(row.get("permission_mode") or 0) % 10 >= 4) else 0
        await ops.execute(
            "INSERT INTO federation_changes(memory_id,changed_at,old_namespace,old_category,"
            "old_public,old_exportable,new_namespace,new_category,new_public,new_exportable) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            row["id"],
            datetime.now(timezone.utc).isoformat(),
            row.get("namespace"),
            row.get("category"),
            public,
            1,
            row.get("namespace"),
            row.get("category"),
            public,
            1,
        )
    # Reserve the sequences here rather than leaving them to the next
    # ``_publish``: a replica whose cursor is already at ``journal:N`` would
    # never see a replayed event that happened to land on N. _publish bumps
    # the clock by the number of rows it assigns, so the re-advertised events
    # are strictly after every sequence already handed out. One _publish call
    # sequences at most _PUBLISH_BATCH rows, so drain until a short batch:
    # otherwise ids past the first batch stay pending and the count below
    # would claim events that were never published.
    published_ids = [row["id"] for row in rows]
    while await _publish(ops, memory_ids=published_ids) == _PUBLISH_BATCH:
        pass
    return len(rows)
