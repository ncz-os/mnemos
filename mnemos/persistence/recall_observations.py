"""Read complete UTC observation intervals through memory repositories."""

from mnemos.persistence.worker_lifecycle import _Ops, transaction_dialect


async def fetch_daily_recall_counts(tx, *, namespace, start, end):
    ops = _Ops(tx, transaction_dialect(tx))
    if ops.dialect != "postgres":
        from mnemos.persistence.base import BackendCapabilityMissing

        raise BackendCapabilityMissing("recall_observations", ops.dialect)
    bucket = {
        "postgres": "date_trunc('day', recalled_at AT TIME ZONE 'UTC')",
        "sqlite": "date(recalled_at)",
        "mysql": "DATE(recalled_at)",
        "oracle": "TRUNC(SYS_EXTRACT_UTC(recalled_at))",
        "db2": "DATE(recalled_at)",
    }[ops.dialect]
    return await ops.fetchall(
        f"SELECT memory_id, {bucket} AS bucket_day, COUNT(*) AS recall_count "
        "FROM memory_recall_log WHERE namespace = ? AND recalled_at >= ? AND recalled_at < ? "
        f"GROUP BY memory_id, {bucket} ORDER BY memory_id, bucket_day",
        namespace,
        start,
        end,
    )


async def read_daily_recall_counts(handle, namespace, start, end):
    if hasattr(handle, "memories"):
        async with handle.transactional() as tx:
            return await handle.memories.fetch_daily_recall_counts(tx, namespace=namespace, start=start, end=end)
    from mnemos.persistence.postgres import PostgresTransaction, PostgresMemoryRepository

    async with handle.acquire() as conn:
        return await PostgresMemoryRepository().fetch_daily_recall_counts(
            PostgresTransaction(conn, None), namespace=namespace, start=start, end=end
        )
