from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mnemos.domain.kronos.anomaly import detect_recall_anomalies


@pytest.mark.asyncio
@pytest.mark.parametrize("hour,minute", [(0, 1), (12, 0), (23, 59)])
@pytest.mark.parametrize("observed", [5, 100, 500])
async def test_compare_complete_recall_days(hour, minute, observed):
    now = datetime(2026, 11, 17, hour, minute, tzinfo=timezone.utc)
    end = now.replace(hour=0, minute=0)
    day = end - timedelta(days=1)
    rows = [{"memory_id": "memory", "bucket_day": day - timedelta(days=n), "recall_count": 100} for n in range(1, 8)]
    rows.append({"memory_id": "memory", "bucket_day": day, "recall_count": observed})
    rows.append({"memory_id": "memory", "bucket_day": end, "recall_count": 0})

    @asynccontextmanager
    async def transactional():
        yield object()

    read = AsyncMock(return_value=rows)
    backend = SimpleNamespace(transactional=transactional, memories=SimpleNamespace(fetch_daily_recall_counts=read))
    result = await detect_recall_anomalies(backend, "notes", now=now)
    assert [item.anomaly_type for item in result] == (
        [] if observed == 100 else ["drop" if observed < 100 else "spike"]
    )
    assert read.await_args.kwargs["start"] == end - timedelta(days=8)
    assert read.await_args.kwargs["end"] == end
