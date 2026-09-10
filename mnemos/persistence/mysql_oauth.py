"""MySQL-family browser/API OAuth operations used by MysqlOAuthRepository.

MySQL previously had no OAuthRepository. This completes its existing contract
as well as the MCP-specific operations shared by all persistence backends.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from mnemos.persistence.base import Transaction
from mnemos.persistence.mcp_oauth import oauth_utc
from mnemos.persistence.types import Row


class MysqlBrowserOAuthMixin:
    async def list_enabled_providers(self, tx: Transaction) -> list[Row]:
        return await self._oauth_fetch_all(
            tx, "SELECT name, display_name, kind, enabled FROM oauth_providers WHERE enabled=1 ORDER BY display_name"
        )

    async def get_provider(self, tx: Transaction, name: str) -> Row | None:
        return await self._mcp_fetch(
            tx,
            "SELECT name, kind, issuer_url, client_id, client_secret, scope, "
            "authorize_url, token_url, userinfo_url, enabled FROM oauth_providers WHERE name=?",
            (name,),
        )

    async def provision_or_link_user(
        self, tx: Transaction, *, provider: str, external_id: str, claims: dict[str, Any]
    ) -> tuple[str, str]:
        existing = await self._mcp_fetch(
            tx,
            "SELECT id, user_id FROM oauth_identities WHERE provider=? AND external_id=?",
            (provider, external_id),
        )
        now = self._mcp_timestamp(datetime.now(timezone.utc))
        raw = json.dumps(claims)
        if existing:
            await self._mcp_execute(
                tx,
                "UPDATE oauth_identities SET last_login_at=?, raw_claims=? WHERE id=?",
                (now, raw, existing["id"]),
            )
            return str(existing["user_id"]), str(existing["id"])
        email = claims.get("email")
        display_name = claims.get("name") or claims.get("preferred_username")
        verified = claims.get("email_verified")
        verified = verified is True or (isinstance(verified, str) and verified.lower().strip() == "true")
        user = None
        if email and verified:
            user = await self._mcp_fetch(tx, "SELECT id FROM users WHERE email=?", (email,))
        user_id = str(user["id"]) if user else None
        if user_id is None:
            # Same collision-resistant principal identity as the other backends.
            from mnemos.core.oauth import _mint_user_id

            user_id = _mint_user_id(provider, external_id)
            try:
                await self._mcp_execute(
                    tx,
                    "INSERT INTO users (id, display_name, email, role) VALUES (?, ?, ?, 'user')",
                    (user_id, display_name, email),
                )
            except Exception as exc:
                if not self._oauth_duplicate(exc):
                    raise
                existing = await self._mcp_fetch(
                    tx,
                    "SELECT id, user_id FROM oauth_identities WHERE provider=? AND external_id=? FOR UPDATE",
                    (provider, external_id),
                )
                if existing:
                    return str(existing["user_id"]), str(existing["id"])
                raise ValueError("OAuth user id collision during provisioning") from exc
        identity_id = uuid.uuid4().hex
        try:
            await self._mcp_execute(
                tx,
                "INSERT INTO oauth_identities "
                "(id, user_id, provider, external_id, email, display_name, raw_claims, last_login_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (identity_id, user_id, provider, external_id, email, display_name, raw, now),
            )
        except Exception as exc:
            if not self._oauth_duplicate(exc):
                raise
            # A competing verified-email login may link the same principal
            # after our initial lookup. Use a locking read to see its commit
            # even under MySQL's default REPEATABLE READ isolation.
            existing = await self._mcp_fetch(
                tx,
                "SELECT id, user_id FROM oauth_identities WHERE provider=? AND external_id=? FOR UPDATE",
                (provider, external_id),
            )
            if existing is None:
                raise
            await self._mcp_execute(
                tx,
                "UPDATE oauth_identities SET last_login_at=?, raw_claims=? WHERE id=?",
                (now, raw, existing["id"]),
            )
            return str(existing["user_id"]), str(existing["id"])
        return user_id, identity_id

    async def create_session(self, tx: Transaction, **kwargs: Any) -> str:
        await self._mcp_execute(
            tx,
            "INSERT INTO oauth_sessions (session_id, user_id, identity_id, expires_at, user_agent, ip_address) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                kwargs["session_id"],
                kwargs["user_id"],
                kwargs["identity_id"],
                self._mcp_timestamp(kwargs["expires_at"]),
                kwargs["user_agent"],
                kwargs["ip_address"],
            ),
        )
        return kwargs["session_id"]

    async def revoke_session(self, tx: Transaction, session_id: str) -> bool:
        changed = await self._mcp_execute(
            tx,
            "UPDATE oauth_sessions SET revoked=1, revoked_at=? WHERE session_id=? AND revoked=0",
            (self._mcp_timestamp(datetime.now(timezone.utc)), session_id),
        )
        return changed > 0

    async def revoke_all_sessions(self, tx: Transaction, user_id: str) -> int:
        return await self._mcp_execute(
            tx,
            "UPDATE oauth_sessions SET revoked=1, revoked_at=? WHERE user_id=? AND revoked=0",
            (self._mcp_timestamp(datetime.now(timezone.utc)), user_id),
        )

    async def get_identity_for_session(self, tx: Transaction, session_id: str) -> Row | None:
        return await self._mcp_fetch(
            tx,
            "SELECT i.id, i.user_id, i.provider, i.external_id, i.email, "
            "i.display_name, i.last_login_at, i.created FROM oauth_sessions s "
            "JOIN oauth_identities i ON i.id=s.identity_id WHERE s.session_id=? AND s.revoked=0",
            (session_id,),
        )

    async def lookup_api_key(self, tx: Transaction, key_hash: str) -> Row | None:
        row = await self._mcp_fetch(
            tx,
            "SELECT ak.id, ak.user_id, ak.revoked, u.role, u.namespace "
            "FROM api_keys ak JOIN users u ON u.id=ak.user_id WHERE ak.key_hash=?",
            (key_hash,),
        )
        if row is None:
            return None
        row["group_ids"] = [
            group["group_id"]
            for group in await self._oauth_fetch_all(
                tx, "SELECT group_id FROM user_groups WHERE user_id=?", (row["user_id"],)
            )
        ]
        return row

    async def touch_api_key(self, tx: Transaction, key_id: Any) -> None:
        await self._mcp_execute(
            tx,
            "UPDATE api_keys SET last_used=? WHERE id=?",
            (self._mcp_timestamp(datetime.now(timezone.utc)), key_id),
        )

    async def resolve_active_session(self, tx: Transaction, session_id: str, *, now: Any) -> Row | None:
        row = await self._mcp_fetch(
            tx,
            "SELECT user_id, identity_id, revoked, expires_at FROM oauth_sessions WHERE session_id=?",
            (session_id,),
        )
        if row is None or row["revoked"] or oauth_utc(row["expires_at"]) <= oauth_utc(now):
            return None
        await self._mcp_execute(
            tx,
            "UPDATE oauth_sessions SET last_used_at=? WHERE session_id=?",
            (self._mcp_timestamp(now), session_id),
        )
        row["expires_at"] = oauth_utc(row["expires_at"])
        return row
