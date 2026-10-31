"""Shared authentication context DTOs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class UserContext:
    user_id: str
    group_ids: List[str]
    role: str
    namespace: str
    authenticated: bool
    session_id: str | None = None
    # api_keys.id this context was resolved from (Bearer path only). Not part
    # of the identity: it exists so revocation can drop the exact cache entry
    # that authenticated with the key.
    api_key_id: str | None = None
