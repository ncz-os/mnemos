"""Validate and mutate exact-content groups within one transaction."""

from mnemos.core import eligibility
from mnemos.persistence import worker_lifecycle


async def mutate_exact_duplicates(
    tx, *, canonical_id, duplicate_ids, owner_id, namespace, content_hash, consolidate=False
):
    ops = worker_lifecycle._Ops(tx, worker_lifecycle.transaction_dialect(tx))
    suffix = {
        "sqlite": "",
        "db2": " FOR UPDATE WITH RS USE AND KEEP UPDATE LOCKS",
    }.get(ops.dialect, " FOR UPDATE")
    fence = eligibility.local_sweep_predicate("") or "1=1"
    rows = {}
    for mid in sorted(set([canonical_id, *duplicate_ids])):
        row = await ops.fetchone("SELECT * FROM memories WHERE id = ?" + suffix, mid)
        if row is not None:
            rows[mid] = row

    def eligible(row):
        return (
            row is not None
            and row.get("owner_id") == owner_id
            and row.get("namespace") == namespace
            and row.get("content_hash") == content_hash
            and all(row.get(key) is None for key in ("deleted_at", "archived_at", "consolidated_into"))
        )

    canonical = rows.get(canonical_id)
    if not eligible(canonical):
        return []
    if not await ops.scalar("SELECT COUNT(*) FROM memories WHERE id = ? AND " + fence, canonical_id):
        return []

    def normalize(value):
        return (value or "").replace("\r\n", "\n").replace("\r", "\n")

    changed = []
    for mid in sorted(set(duplicate_ids) - {canonical_id}):
        row = rows.get(mid)
        if not eligible(row) or normalize(row.get("content")) != normalize(canonical.get("content")):
            continue
        fields = "deleted_at = CURRENT_TIMESTAMP, updated = CURRENT_TIMESTAMP"
        params = []
        if consolidate:
            fields += ", consolidated_into = ?, consolidated_at = CURRENT_TIMESTAMP"
            params.append(canonical_id)
        count = await ops.execute(
            "UPDATE memories SET " + fields + " WHERE id = ? AND deleted_at IS NULL AND " + fence, *params, mid
        )
        if count:
            changed.append(row)
    return changed
