from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from mnemos.persistence.base import ALL_CAPABILITIES
from mnemos.persistence.db2 import Db2Backend, Db2ConsultationsRepository, Db2OAuthRepository, Db2SessionsRepository
from mnemos.persistence.sqlite import SqliteBackend


def _settings() -> SimpleNamespace:
    return SimpleNamespace(database=SimpleNamespace(embedding_dim=768, db2_dialect="compat"))


@pytest.mark.asyncio
async def test_db2_protocol_parity_matches_sqlite_facade_shape(tmp_path: Path) -> None:
    backend = SqliteBackend(tmp_path / "mnemos.db", _settings())
    await backend.open()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    token = bytes.fromhex("33" * 32)
    state = bytes.fromhex("44" * 32)
    session_id = uuid.uuid4()
    consultation_id = uuid.uuid4()

    try:
        async with backend.transactional() as tx:
            oauth = await backend.register_oauth_token(
                tx,
                token=token,
                user_id="alice",
                provider="github",
                scopes=["openid", "email"],
                expires_at=expires_at,
                refresh_token="refresh",
            )
            assert oauth["token"] == token.hex()
            assert oauth["scopes"] == ["openid", "email"]
            assert (await backend.lookup_oauth_token(tx, token=token))["last_used_at"] is not None

            flow = await backend.start_oauth_flow(
                tx,
                state=state,
                provider="github",
                csrf_token="csrf",
                return_url="/return",
                expires_at=expires_at,
            )
            assert flow["state"] == state.hex()
            assert (await backend.redeem_oauth_state(tx, state=state))["provider"] == "github"
            assert await backend.redeem_oauth_state(tx, state=state) is None

            session = await backend.create_session(
                tx,
                session_id=session_id,
                user_id="alice",
                expires_at=expires_at,
                metadata={"ip": "127.0.0.1"},
            )
            assert session["session_id"] == str(session_id)
            assert session["metadata"] == {"ip": "127.0.0.1"}
            assert await backend.update_session_active(tx, session_id=session_id) is True

            event = await backend.log_session_event(
                tx,
                session_id=session_id,
                event_kind="login",
                payload={"provider": "github"},
            )
            assert event["payload"] == {"provider": "github"}

            consultation = await backend.create_consultation(
                tx,
                consultation_id=consultation_id,
                user_id="alice",
                prompt="summarize",
                task_type="reasoning",
                mode="auto",
            )
            assert consultation["id"] == str(consultation_id)
            response = await backend.append_consultation_response(
                tx,
                consultation_id=consultation_id,
                provider="openai",
                model_id="gpt-test",
                response="done",
                final_score=0.91,
                tokens_in=7,
                tokens_out=3,
                latency_ms=42,
            )
            assert response["consultation_id"] == str(consultation_id)
            fetched = await backend.fetch_consultation(tx, consultation_id=consultation_id)
            assert fetched is not None
            assert fetched["responses"][0]["response"] == "done"
            assert [row["id"] for row in await backend.list_consultations(tx, user_id="alice")] == [
                str(consultation_id)
            ]
            assert await backend.expire_session(tx, session_id=session_id) is True
            assert await backend.lookup_session(tx, session_id=session_id) is None
            assert await backend.revoke_oauth_token(tx, token=token) is True
    finally:
        await backend.close()


def test_db2_backend_advertises_protocols_and_methods() -> None:
    backend = Db2Backend(pool=object(), settings=_settings())

    assert backend.capabilities == set(ALL_CAPABILITIES)
    for name in (
        "register_oauth_token",
        "lookup_oauth_token",
        "revoke_oauth_token",
        "start_oauth_flow",
        "redeem_oauth_state",
        "create_session",
        "lookup_session",
        "update_session_active",
        "expire_session",
        "log_session_event",
        "create_consultation",
        "append_consultation_response",
        "fetch_consultation",
        "list_consultations",
    ):
        assert callable(getattr(backend, name))


def test_db2_0038_migration_contains_guarded_native_schema() -> None:
    root = Path(__file__).resolve().parents[2]
    sql = (root / "db/migrations_db2/0038_oauth_sessions_consultations.sql").read_text()

    for table in (
        "oauth_tokens",
        "oauth_state",
        "sessions",
        "session_logs",
        "consultations",
        "consultation_responses",
    ):
        assert table in sql
    assert "SYSCAT.TABLES" in sql
    assert "SYSCAT.COLUMNS" in sql
    assert "CHAR(32) FOR BIT DATA" in sql
    assert "CHAR(16) FOR BIT DATA" in sql
    assert "CURRENT TIMESTAMP" in sql


class _RecordingCursor:
    rowcount = 1

    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []
        self.description: list[tuple[str]] = []

    def execute(self, sql: str, params: object = ()) -> None:
        self.statements.append((sql, params))
        self.description = []

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        return []

    def close(self) -> None:
        return None


class _RecordingConn:
    def __init__(self) -> None:
        self.cursor_obj = _RecordingCursor()

    def cursor(self) -> _RecordingCursor:
        return self.cursor_obj


@pytest.mark.asyncio
async def test_db2_repositories_emit_native_positional_sql() -> None:
    conn = _RecordingConn()
    token = bytes.fromhex("55" * 32)
    session_id = uuid.uuid4()

    assert await Db2OAuthRepository().lookup_oauth_token(conn, token=token) is None
    assert await Db2OAuthRepository().redeem_oauth_state(conn, state=token) is None
    assert await Db2SessionsRepository().lookup_session(conn, session_id=session_id) is None
    assert await Db2SessionsRepository().expire_session(conn, session_id=session_id) is True
    assert await Db2ConsultationsRepository().list_consultations(conn, user_id="alice") == []

    sql = "\n".join(statement for statement, _ in conn.cursor_obj.statements)
    assert "SYSTIMESTAMP" not in sql
    assert "RETURNING" not in sql
    assert ":token" not in sql
    assert ":session_id" not in sql
    assert "CURRENT TIMESTAMP" in sql
    assert "?" in sql
