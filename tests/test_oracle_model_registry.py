from types import SimpleNamespace

import pytest

from mnemos.persistence.oracle import OracleConsultationAuditRepository


@pytest.mark.asyncio
async def test_oracle_fetch_model_provider_queries_active_registry_row() -> None:
    calls = []

    class Cursor:
        description = (("PROVIDER",),)

        async def execute(self, sql, params):
            calls.append((sql, params))

        async def fetchone(self):
            return ("provider-a",)

        async def close(self):
            return None

    class Conn:
        def cursor(self):
            return Cursor()

    provider = await OracleConsultationAuditRepository().fetch_model_provider(
        SimpleNamespace(conn=Conn()), "model-a"
    )

    assert provider == "provider-a"
    assert "available = 1" in calls[0][0]
    assert "deprecated = 0" in calls[0][0]
    assert "ROWNUM = 1" in calls[0][0]
    assert calls[0][1] == {"m": "model-a"}
