"""Shared ownership boundary for destructive MORPHEUS consolidation."""

from collections import defaultdict
from typing import Any

from .worker_lifecycle import _Ops, transaction_dialect


async def partition_consolidation_clusters(tx: Any, clusters: list[dict], *, dialect: str | None = None) -> list[dict]:
    """Lock current ownership, then split even stale/mixed persisted clusters.

    Include previously consolidated rows: they are needed for idempotent
    repeat counts, but must never supply another owner's minimum cluster
    size. All locks remain held by the caller's phase transaction.
    """
    dialect = dialect or transaction_dialect(tx)
    ops = _Ops(tx, dialect)
    ids = sorted({str(mid) for cluster in clusters for mid in cluster.get("member_memory_ids", []) if mid})
    owners = {}
    for offset in range(0, len(ids), 400):
        batch = ids[offset : offset + 400]
        placeholders = ",".join("?" for _ in batch)
        lock = "" if dialect == "sqlite" else " FOR UPDATE"
        rows = await ops.fetchall(
            f"SELECT id, owner_id, namespace FROM memories WHERE id IN ({placeholders}) ORDER BY id{lock}",
            *batch,
        )
        owners.update({str(row["id"]): (row["owner_id"], row["namespace"]) for row in rows})
    result = []
    for cluster in clusters:
        groups = defaultdict(list)
        for mid in dict.fromkeys(str(mid) for mid in cluster.get("member_memory_ids", []) if mid):
            if mid in owners:
                groups[owners[mid]].append(mid)
        result.extend({**cluster, "member_memory_ids": members} for members in groups.values())
    return result
