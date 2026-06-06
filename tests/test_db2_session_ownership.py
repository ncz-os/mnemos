"""Lock in the DB2 browser-vs-chat session ownership boundary.

GRAEAE architecture verdict (consensus 1.0): browser/OAuth-session operations
live on the backend facade (auth surface); the chat ``SessionsRepository`` owns
only conversational state. These offline introspection probes prevent the
DB2 backend from regressing into the prior conflation, where
``Db2SessionsRepository`` carried browser-session methods that shadowed the
inherited chat ``create_session``.

No database required.
"""

from __future__ import annotations

import pytest

# Browser/OAuth-session ops must be facade-owned; chat ops repo-owned.
_BROWSER_SESSION_METHODS = (
    "create_session",
    "lookup_session",
    "update_session_active",
    "expire_session",
    "log_session_event",
)


def _db2():
    db2 = pytest.importorskip("mnemos.persistence.db2")
    return db2


def test_db2_backend_facade_owns_browser_session_methods() -> None:
    """The browser-session ops are defined directly on Db2Backend (mirroring
    OracleBackend), not delegated to / inherited from a repository."""
    db2 = _db2()
    for name in _BROWSER_SESSION_METHODS:
        assert name in vars(db2.Db2Backend), f"Db2Backend must own browser-session method {name!r} on the facade"


def test_db2_sessions_repository_is_pure_chat() -> None:
    """Db2SessionsRepository must NOT define the browser-session methods; it is
    a chat repository that inherits the chat contract from OracleSessionsRepository."""
    db2 = _db2()
    own = vars(db2.Db2SessionsRepository)
    leaked = [m for m in _BROWSER_SESSION_METHODS if m in own]
    assert not leaked, (
        f"Db2SessionsRepository leaks browser-session methods {leaked}; "
        f"these belong on the Db2Backend facade (browser/auth surface)."
    )
    # create_session must resolve to the chat SessionsRepository contract, not a
    # browser override — i.e. it is inherited, not defined on the subclass.
    assert "create_session" not in own


def test_db2_backend_browser_create_session_signature() -> None:
    """The facade create_session keeps the browser-session signature
    (session_id/user_id/expires_at/metadata), distinct from the chat ABC."""
    import inspect

    db2 = _db2()
    params = set(inspect.signature(db2.Db2Backend.create_session).parameters)
    assert {"session_id", "user_id", "expires_at", "metadata"} <= params
