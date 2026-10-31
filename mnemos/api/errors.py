"""Exception-to-HTTP mapping for route handlers.

Route modules were each mapping the same persistence exceptions by hand:
``NotImplementedError`` -> 503, a lost backend connection -> 503 with the
``database_disconnected`` body, anything else -> 500 with a generic detail.
Every copy drifted slightly (some logged, some did not; one returned the
exception text, another a fixed string). This module is the single mapping
so a route writes ``raise translate_persistence_error(exc) from exc`` and
gets the fleet-wide shape.

Also here: the disconnect classifier. It walks the exception and its
``__cause__``/``__context__`` chain looking for known DB-driver class names
or text markers, so a route does not need to import every driver to tell
"the database went away" from "my query is wrong". It moved here from
``mnemos/api/routes/federation.py`` unchanged; that route imports it back.

Nothing here changes a status code or a body that existed before; the
helpers reproduce the mappings the routes already applied.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Class names + error strings that indicate the DB backend lost its
# connection (vs a legitimate query failure / programming error).
# Matched by name + chained-cause walk so we don't hard-import every
# DB driver. Triggered by the 2026-05-23 a GPU host investigation
# (hive job 019e563f): mnemos-api-cerb's Oracle standby was MOUNTED
# (not OPEN READ ONLY), oracledb raised DPY-6005 inside feed_query,
# raw 500 propagated to the production host federation pull worker.
_DB_DISCONNECT_EXC_NAMES = frozenset(
    {
        "OperationalError",  # oracledb + sqlalchemy + psycopg
        "InterfaceError",  # oracledb + psycopg
        "ConnectionDoesNotExistError",  # asyncpg
        "ConnectionRefusedError",  # builtin
        "DisconnectionError",  # sqlalchemy
        "CannotConnectNowError",  # asyncpg
    }
)
_DB_DISCONNECT_TEXT_MARKERS = (
    "dpy-6005",
    "cannot connect",
    "connection refused",
    "connection reset",
    "connection lost",
    "no listener",
    "ora-12541",  # TNS no listener
    "ora-12514",  # listener doesn't currently know
    "could not translate host name",
)


def is_db_disconnect(exc: BaseException) -> bool:
    """Walk the exception + its __cause__/__context__ chain matching
    known DB-disconnect class names + text markers. Returns True when
    the failure looks like the backend lost its connection rather than
    a programming error."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if type(cur).__name__ in _DB_DISCONNECT_EXC_NAMES:
            return True
        text = str(cur).lower()
        if any(marker in text for marker in _DB_DISCONNECT_TEXT_MARKERS):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def database_disconnected_detail(exc: BaseException) -> dict:
    """Body for a 503 caused by a lost backend connection."""
    return {
        "error": "database_disconnected",
        "exception_type": type(exc).__name__,
        "message": str(exc),
    }


def translate_persistence_error(exc: BaseException) -> HTTPException:
    """Map a persistence-layer exception to the HTTP error a route raises.

    * ``NotImplementedError`` -> 503: the capability is advertised but the
      active backend has no implementation.
    * a lost connection -> 503 with the ``database_disconnected`` body.
    * everything else -> 500 with a fixed detail (exception text is not
      client-visible).
    """
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, NotImplementedError):
        return HTTPException(status_code=503, detail=str(exc))
    if is_db_disconnect(exc):
        return HTTPException(status_code=503, detail=database_disconnected_detail(exc))
    return HTTPException(status_code=500, detail="Internal server error")
