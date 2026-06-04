from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest

MYSQL_DSN = os.environ.get("MYSQL_DSN")

pytestmark = [
    pytest.mark.skipif(not MYSQL_DSN, reason="MYSQL_DSN not set; live probe skipped"),
    pytest.mark.asyncio,
]


async def test_mysql_consultation_audit_live_roundtrip() -> None:
    pytest.importorskip("aiomysql", reason="aiomysql driver not installed")

    from mnemos.persistence.mysql import MysqlBackend, create_mysql_pool

    pool = await create_mysql_pool(MYSQL_DSN)
    backend = MysqlBackend(pool, SimpleNamespace())
    await backend.open()

    run_id = uuid.uuid4().hex[:12]
    owner_id = f"mysql_consaudit_live_{run_id}"
    namespace = f"ns_{run_id}"
    provider = "nvidia"
    model_id = f"qwen/qwen3-coder-480b-live-{run_id}"
    consultation_id: str | None = None
    audit_id: str | None = None

    try:
        async with backend.transactional() as tx:
            async with tx.conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO model_registry (
                        provider, model_id, display_name, family, capabilities,
                        input_cost_per_mtok, output_cost_per_mtok, context_window,
                        graeae_weight, available, deprecated
                    ) VALUES (
                        %s, %s, %s, %s, CAST(%s AS JSON),
                        %s, %s, %s,
                        %s, TRUE, FALSE
                    )
                    ON DUPLICATE KEY UPDATE
                        display_name = VALUES(display_name),
                        family = VALUES(family),
                        capabilities = VALUES(capabilities),
                        input_cost_per_mtok = VALUES(input_cost_per_mtok),
                        output_cost_per_mtok = VALUES(output_cost_per_mtok),
                        context_window = VALUES(context_window),
                        graeae_weight = VALUES(graeae_weight),
                        available = TRUE,
                        deprecated = FALSE
                    """,
                    (
                        provider,
                        model_id,
                        "Live MySQL Coding Model",
                        "qwen",
                        '["coding"]',
                        0.00,
                        0.00,
                        128000,
                        1.00,
                    ),
                )

            recommended, required = await backend.consultations_audit.fetch_recommended_model(
                tx,
                "coding",
                10.0,
                0.85,
            )
            fetched_recommendation = await backend.consultations_audit.fetch_model_recommendation(tx, "coding")
            assert recommended is not None
            assert fetched_recommendation is not None
            assert recommended["provider"] == provider
            assert fetched_recommendation["model_id"] == model_id
            assert "coding" in required
            assert await backend.consultations_audit.lookup_provider_for_model(tx, model_id) == provider
            assert await backend.consultations_audit.fetch_model_provider(tx, model_id) == provider
            available = await backend.consultations_audit.fetch_available_models(tx)
            assert any(row["provider"] == provider and row["model_id"] == model_id for row in available)

            consultation_id = await backend.consultations_audit.create_consultation_with_audit(
                tx,
                prompt="live MySQL consultation audit prompt",
                task_type="coding",
                consensus_response="live MySQL consultation audit response",
                consensus_score=0.91,
                winning_muse=provider,
                cost=0.03,
                latency_ms=42,
                mode="single",
                owner_id=owner_id,
                namespace=namespace,
                memory_ids=[],
                genesis_hash="0" * 64,
            )
            consultation = await backend.consultations_audit.fetch_consultation_by_id(tx, consultation_id)
            assert consultation is not None
            assert consultation["id"] == consultation_id

            audit_id = await backend.consultations_audit.insert_consultation_audit(
                tx,
                consultation_id=consultation_id,
                prompt="live MySQL direct audit prompt",
                provider=provider,
                model=model_id,
                response_text="live MySQL direct audit response",
                task_type="coding",
                quality_score=0.92,
                latency_ms=43,
                cost_usd=0.04,
                genesis_hash="0" * 64,
            )
            audit = await backend.consultations_audit.fetch_consultation_audit(tx, audit_id)
            audits = await backend.consultations_audit.fetch_consultation_audits(
                tx,
                consultation_id=consultation_id,
                limit=10,
                offset=0,
            )
            scoped = await backend.consultations_audit.list_audit_log(
                tx,
                root=False,
                user_id=owner_id,
                namespace=namespace,
                limit=10,
                offset=0,
            )
            assert audit is not None
            assert audit["id"] == audit_id
            assert audit["provider"] == provider
            assert any(row["id"] == audit_id for row in audits)
            assert any(row["consultation_id"] == consultation_id for row in scoped)
    finally:
        try:
            async with backend.transactional() as tx:
                async with tx.conn.cursor() as cursor:
                    if consultation_id is not None:
                        await cursor.execute(
                            "DELETE FROM consultation_memory_refs WHERE consultation_id = %s",
                            (consultation_id,),
                        )
                        await cursor.execute(
                            "DELETE FROM graeae_audit_log WHERE consultation_id = %s",
                            (consultation_id,),
                        )
                    if audit_id is not None:
                        await cursor.execute("DELETE FROM graeae_audit_log WHERE id = %s", (audit_id,))
                    await cursor.execute(
                        "DELETE FROM graeae_consultations WHERE owner_id = %s AND namespace = %s",
                        (owner_id, namespace),
                    )
                    await cursor.execute(
                        "DELETE FROM model_registry WHERE provider = %s AND model_id = %s",
                        (provider, model_id),
                    )
        finally:
            await backend.close()
