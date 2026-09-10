"""Shared MCP OAuth repository semantics; drivers supply native SQL bindings.

All operations use the caller's transaction, including refresh-family locking.
The existing PostgreSQL table and column names remain the storage contract.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from mnemos.persistence.base import Transaction
from mnemos.persistence.types import Row


_OPAQUE_ID = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)


def _valid_opaque_id(value: Any) -> bool:
    # All issued client IDs, codes, JTIs and refresh hashes are URL-safe ASCII.
    # Reject alterations before SQL: Db2 VARCHAR and some MySQL collations
    # consider trailing spaces equal, unlike PostgreSQL/SQLite. Never strip.
    return isinstance(value, str) and _OPAQUE_ID.fullmatch(value) is not None


def oauth_utc(value: Any) -> datetime:
    """Decode driver timestamps as aware UTC; timezone-less DB columns are UTC."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("stored OAuth timestamp is invalid")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class MCPOAuthRepositoryMixin:
    """Portable, transaction-scoped OAuth operations using bound parameters.

    SQLite transactions begin IMMEDIATE. Other adapters provide locking reads;
    those reads deliberately bypass MySQL's repeatable-read snapshot after
    waiting on the immutable family root.
    """

    _mcp_lock_suffix = " FOR UPDATE"
    _mcp_insert_key_suffix = " ON CONFLICT (key_id) DO NOTHING"

    async def _mcp_fetch(self, tx: Transaction, sql: str, params: tuple = ()) -> Row | None:
        raise NotImplementedError

    async def _mcp_execute(self, tx: Transaction, sql: str, params: tuple = ()) -> int:
        raise NotImplementedError

    def _mcp_timestamp(self, value: Any) -> Any:
        return oauth_utc(value)

    async def mcp_get_signing_key(self, tx: Transaction) -> str | None:
        row = await self._mcp_fetch(tx, "SELECT signing_key FROM oauth_mcp_signing_keys WHERE key_id = ?", ("default",))
        return row["signing_key"] if row else None

    async def mcp_save_signing_key(self, tx: Transaction, *, key_id: str, signing_key: str) -> None:
        await self._mcp_execute(
            tx,
            "INSERT INTO oauth_mcp_signing_keys (key_id, signing_key) VALUES (?, ?)" + self._mcp_insert_key_suffix,
            (key_id, signing_key),
        )

    async def mcp_save_client(self, tx: Transaction, row: Row) -> None:
        uris = row["redirect_uris"]
        if isinstance(uris, list):
            uris = json.dumps(uris)
        await self._mcp_execute(
            tx,
            "INSERT INTO oauth_mcp_clients "
            "(client_id, client_secret, redirect_uris, token_endpoint_auth_method) VALUES (?, ?, ?, ?)",
            (row["client_id"], row.get("client_secret"), uris, row["token_endpoint_auth_method"]),
        )

    async def mcp_get_client(self, tx: Transaction, client_id: str) -> Row | None:
        if not _valid_opaque_id(client_id):
            return None
        row = await self._mcp_fetch(
            tx,
            "SELECT client_id, client_secret, redirect_uris, token_endpoint_auth_method "
            "FROM oauth_mcp_clients WHERE client_id = ?",
            (client_id,),
        )
        if row is None:
            return None
        client = dict(row)
        uris = client["redirect_uris"]
        if isinstance(uris, str):
            uris = json.loads(uris)
        if not isinstance(uris, list) or not all(isinstance(uri, str) for uri in uris):
            raise ValueError("stored OAuth client redirect_uris is invalid")
        client["redirect_uris"] = uris
        return client

    async def mcp_save_code(self, tx: Transaction, row: Row) -> None:
        await self._mcp_execute(
            tx,
            "INSERT INTO oauth_mcp_authorization_codes "
            "(code, client_id, code_challenge, code_challenge_method, redirect_uri, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                row["code"],
                row["client_id"],
                row["code_challenge"],
                row["code_challenge_method"],
                row["redirect_uri"],
                self._mcp_timestamp(row["expires_at"]),
            ),
        )

    async def mcp_consume_code(self, tx: Transaction, code: str) -> Row | None:
        if not _valid_opaque_id(code):
            return None
        now = self._mcp_timestamp(datetime.now(timezone.utc))
        changed = await self._mcp_execute(
            tx,
            "UPDATE oauth_mcp_authorization_codes SET used_at = ? "
            "WHERE code = ? AND used_at IS NULL AND expires_at > ?",
            (now, code, now),
        )
        if not changed:
            return None
        row = await self._mcp_fetch(
            tx,
            "SELECT code, client_id, code_challenge, code_challenge_method, redirect_uri, expires_at "
            "FROM oauth_mcp_authorization_codes WHERE code = ?",
            (code,),
        )
        if row is not None:
            row = dict(row)
            row["expires_at"] = oauth_utc(row["expires_at"])
        return row

    async def mcp_save_token(self, tx: Transaction, row: Row) -> None:
        await self._mcp_execute(
            tx,
            "INSERT INTO oauth_mcp_tokens "
            "(jti, refresh_token_hash, client_id, family_id, parent_jti, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                row["jti"],
                row["refresh_token_hash"],
                row["client_id"],
                row["family_id"],
                row.get("parent_jti"),
                self._mcp_timestamp(row["expires_at"]),
            ),
        )

    async def mcp_rotate_refresh(self, tx: Transaction, token_hash: str, client_id: str, successor: Row) -> str:
        if not _valid_opaque_id(token_hash) or not _valid_opaque_id(client_id):
            return "invalid"
        presented = await self._mcp_fetch(
            tx,
            "SELECT family_id FROM oauth_mcp_tokens WHERE refresh_token_hash = ? AND client_id = ?",
            (token_hash, client_id),
        )
        if presented is None:
            return "invalid"
        # Every rotation and ancestor replay takes the same immutable root lock.
        # The root remains present after rotation and after family revocation.
        root = await self._mcp_fetch(
            tx,
            "SELECT jti FROM oauth_mcp_tokens WHERE jti = ?" + self._mcp_lock_suffix,
            (presented["family_id"],),
        )
        if root is None:
            return "invalid"
        current = await self._mcp_fetch(
            tx,
            "SELECT jti, family_id, revoked_at, expires_at FROM oauth_mcp_tokens "
            "WHERE refresh_token_hash = ? AND client_id = ?" + self._mcp_lock_suffix,
            (token_hash, client_id),
        )
        if current is None:
            return "invalid"
        now = datetime.now(timezone.utc)
        stamp = self._mcp_timestamp(now)
        if current["revoked_at"] is not None:
            await self._mcp_execute(
                tx,
                "UPDATE oauth_mcp_tokens SET revoked_at = COALESCE(revoked_at, ?) WHERE family_id = ?",
                (stamp, current["family_id"]),
            )
            return "reused"
        if oauth_utc(current["expires_at"]) <= now:
            return "invalid"
        await self._mcp_execute(
            tx,
            "UPDATE oauth_mcp_tokens SET revoked_at = ?, replaced_by_jti = ? WHERE jti = ?",
            (stamp, successor["jti"], current["jti"]),
        )
        successor["family_id"] = current["family_id"]
        successor["parent_jti"] = current["jti"]
        await self.mcp_save_token(tx, successor)
        return "rotated"
