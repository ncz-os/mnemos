"""Tests for scripts/backfill_embeddings_http.py's empty-URL guard.

The script's embedding endpoint used to carry a default host that no
longer exists; ``MNEMOS_EMBED_HTTP_URL`` now defaults to "". Without a
guard, an unconfigured run would connect to the database, fetch a batch,
POST to an empty URL, and exit 1 with a generic "embed endpoint failing"
message — blaming the endpoint for what is really missing configuration.

These tests lock in the fail-fast behaviour:

  * ``_validate_embed_url("")`` (and whitespace-only) raises SystemExit
    with a non-zero code and a message naming MNEMOS_EMBED_HTTP_URL.
  * the guard runs BEFORE any database connection is attempted, so an
    unconfigured script never opens a DB session.
  * a valid http(s) URL is accepted (stripped) and the normal path still
    POSTs to it.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_embeddings_http.py"


def _load_script():
    """Import the script as a module without running its __main__ block."""
    spec = importlib.util.spec_from_file_location("backfill_embeddings_http_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_script()


@pytest.mark.parametrize("url", ["", "   ", "\t\n"])
def test_validate_embed_url_rejects_empty(script, capsys, url):
    with pytest.raises(SystemExit) as exc:
        script._validate_embed_url(url)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "MNEMOS_EMBED_HTTP_URL" in err
    assert "no embedding endpoint configured" in err


def test_validate_embed_url_rejects_non_http_scheme(script, capsys):
    with pytest.raises(SystemExit) as exc:
        script._validate_embed_url("ftp://example/v1/embeddings")
    assert exc.value.code != 0
    assert "http" in capsys.readouterr().err


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8090/v1/embeddings", "https://embed.example/v1/embeddings"],
)
def test_validate_embed_url_accepts_http_and_strips(script, url):
    assert script._validate_embed_url(f"  {url}  ") == url


def test_main_exits_before_connecting_when_no_embed_url(monkeypatch, capsys):
    """With MNEMOS_EMBED_HTTP_URL unset the script must exit non-zero and
    must NOT touch the database at all (no connect() call)."""
    script = _load_script()
    monkeypatch.delenv("MNEMOS_EMBED_HTTP_URL", raising=False)
    monkeypatch.setattr(script, "EMBED_URL", "")

    connected = {"count": 0}

    class _Backend:
        scheme = "oracle"

        def connect(self):
            connected["count"] += 1
            raise AssertionError("must not connect when no embed URL is configured")

    monkeypatch.setattr(script, "_make_backend", lambda _dsn: _Backend())

    with pytest.raises(SystemExit) as exc:
        asyncio.run(script.main())

    assert exc.value.code != 0
    assert connected["count"] == 0
    err = capsys.readouterr().err
    assert "MNEMOS_EMBED_HTTP_URL" in err


def test_embed_batch_posts_to_given_url(script, monkeypatch):
    """embed_batch() must POST to the URL passed by main(), not the
    module-level constant read at import time."""
    captured = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}

    class _Client:
        async def post(self, url, json=None, timeout=None):  # noqa: A002 - mirrors httpx
            captured["url"] = url
            captured["json"] = json
            return _Resp()

    monkeypatch.setattr(script, "EMBED_URL", "")
    vectors, _dt = asyncio.run(script.embed_batch(_Client(), ["hello"], "http://embed.example/v1/embeddings"))
    assert captured["url"] == "http://embed.example/v1/embeddings"
    assert captured["json"]["input"] == ["hello"]
    assert vectors == [[0.1, 0.2]]


# ── deterministic batch pagination ──────────────────────────────────────
# ``main`` loops ``fetch_batch`` until it returns nothing, and the WHERE
# clause (``embedding IS NULL``) does not exclude a row until its update
# commits. Without an explicit ORDER BY the DB2 and Oracle backends hand
# back whatever order the engine feels like, so a long run can re-read the
# same rows while other rows are never reached. Postgres already ordered;
# the sync backends now match it.


class _RecordingCursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.sql = None

    def execute(self, sql, *args):
        self.sql = sql

    def fetchall(self):
        return [tuple(r) for r in self.rows]

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def close(self):
        return None


class _RecordingConn:
    def __init__(self, rows=()):
        self.cursor_obj = _RecordingCursor(rows)

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        return None

    def close(self):
        return None


@pytest.mark.parametrize("backend_name", ["_OracleBackend", "_Db2Backend"])
def test_fetch_batch_orders_by_id_for_deterministic_pagination(script, backend_name):
    backend = object.__new__(getattr(script, backend_name))
    conn = _RecordingConn([("mem_1", "hello world")])
    out = backend.fetch_batch(conn, 10)
    sql = conn.cursor_obj.sql
    assert "ORDER BY id" in sql, sql
    # ORDER BY must precede the row-limit clause, or Oracle/Db2 reject it.
    assert sql.index("ORDER BY id") < sql.index("FETCH FIRST")
    assert out == [("mem_1", "hello world")]


def test_fetch_batch_pagination_statements_agree_across_backends(script):
    """Postgres and the sync backends must page identically: same order key."""
    ora_conn = _RecordingConn()
    object.__new__(script._OracleBackend).fetch_batch(ora_conn, 5)
    db2_conn = _RecordingConn()
    object.__new__(script._Db2Backend).fetch_batch(db2_conn, 5)
    ora_sql = " ".join(ora_conn.cursor_obj.sql.split())
    db2_sql = " ".join(db2_conn.cursor_obj.sql.split())
    assert ora_sql == db2_sql
    assert "ORDER BY id" in ora_sql
