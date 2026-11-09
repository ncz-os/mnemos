"""F2: document-import ``memory.created`` webhooks must not leak raw secrets.

The document-import route classifies every chunk through
``DocumentRepository.import_chunk`` (which routes through
``classify_persisted_text_fields``), so a chunk carrying a credential is
stored in the vault namespace with the secret spans recorded in its
metadata.  The ``memory.created`` webhook emitted right after the write used
to send the RAW chunk content and the CALLER's namespace, leaking both the
credential and the pre-vault namespace to an external HTTP endpoint.

These tests use a real ``SqliteBackend`` so the actual classification and
storage happen, stub only the document parser, and capture the payload the
route hands to the transactional outbox (``dispatch_event``).  That payload
is exactly what leaves the server, so asserting on it asserts on the leak.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from mnemos.api.routes import document_import
from mnemos.core.secret_detection import VAULT_NAMESPACE

# NOTE: async tests carry an explicit ``@pytest.mark.asyncio`` rather than a
# module-level ``pytestmark``. The static AST guards appended at the bottom of
# this module are synchronous, and a module-level asyncio mark would claim
# (and warn about) them.
async_test = pytest.mark.asyncio

# Synthetic, non-functional credentials. The chunk is a single text span so
# the classifier records both spans and vaults the record (a credential-record
# memory rather than incidental prose).
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
GITHUB_TOKEN = "ghp_" + "A" * 36
SECRET_CHUNK = f"deploy notes: aws key {AWS_KEY} and github token {GITHUB_TOKEN} end"


class _User:
    """Minimal UserContext: a non-root caller in a non-vault namespace."""

    user_id = "alice"
    namespace = "team"
    role = "user"
    group_ids: list[str] = []
    authenticated = True
    api_key_id = None


class _Upload:
    """Stand-in for ``fastapi.UploadFile`` (only what the route reads)."""

    def __init__(self, filename: str, data: bytes) -> None:
        self.filename = filename
        self._data = data

    async def read(self, size: int = -1) -> bytes:
        data, self._data = self._data, b""
        return data


class _CapturingWebhooks:
    """Wrap a real webhook repo and capture the payloads the route emits."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[dict] = []

    async def dispatch_event(self, tx, event_type, payload, *, owner_id=None, namespace=None):
        self.calls.append(
            {
                "event_type": event_type,
                "payload": payload,
                "owner_id": owner_id,
                "namespace": namespace,
            }
        )
        return []


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path, monkeypatch):
    import mnemos.core.lifecycle as lc
    from mnemos.persistence import SqliteBackend

    backend = SqliteBackend(tmp_path / "doc_import_webhook.sqlite3", SimpleNamespace())
    await backend.open()
    monkeypatch.setattr(lc, "_persistence_backend", backend, raising=False)
    monkeypatch.setattr(lc, "_cache", None, raising=False)
    try:
        yield backend
    finally:
        await backend.close()


def _stub_parser(monkeypatch, chunks):
    importer = MagicMock()
    importer.parse_document.return_value = (
        "doc text",
        {"source_file": "doc.pdf"},
        chunks,
    )
    monkeypatch.setattr(document_import, "DOCLING_AVAILABLE", True)
    monkeypatch.setattr(document_import, "DoclingImporter", lambda: importer)
    return importer


async def _run_import(backend, monkeypatch, content: str):
    """Drive the route end to end and return (payload, captured webhooks)."""
    _stub_parser(
        monkeypatch,
        [
            {
                "chunk_num": 0,
                "title": "Chunk 0",
                "content": content,
                "metadata": {"source_file": "doc.pdf", "chunk_num": 0},
            }
        ],
    )
    capturing = _CapturingWebhooks(backend.webhooks)
    monkeypatch.setattr(type(backend), "webhooks", property(lambda _self: capturing))
    monkeypatch.setattr(backend, "supports_webhooks", True, raising=False)

    payload, status_code = await document_import.import_memories_from_document(
        file=_Upload("doc.pdf", b"%PDF-1.4\nx"),
        category="documents",
        subcategory=None,
        permission_mode=None,
        project_tag="mnemos",
        allow_archive_snapshot=False,
        user=_User(),
    )
    return payload, status_code, capturing


@async_test
async def test_webhook_masks_secrets_and_uses_stored_namespace(sqlite_backend, monkeypatch):
    """A vaulted chunk's webhook carries masked content and namespace=vault."""
    payload, status_code, capturing = await _run_import(sqlite_backend, monkeypatch, SECRET_CHUNK)

    assert status_code == 200, payload
    assert payload["memories_created"] == 1
    assert len(capturing.calls) == 1

    event = capturing.calls[0]
    assert event["event_type"] == "memory.created"
    data = event["payload"]

    # The raw credentials must never reach the outbound payload.
    assert AWS_KEY not in data["content"]
    assert GITHUB_TOKEN not in data["content"]
    assert "[REDACTED" in data["content"] or "REDACTED" in data["content"]

    # The event must describe the row as stored, not the caller's namespace.
    assert data["namespace"] == VAULT_NAMESPACE
    assert data["namespace"] != "team"

    # Sanity: the row really was vaulted (this is what makes the payload lie).
    from mnemos.persistence.visibility import VisibilityFilter

    async with sqlite_backend.transactional() as tx:
        row = await sqlite_backend.memories.get_memory(
            tx,
            payload["memory_ids"][0],
            visibility=VisibilityFilter.for_read(_Root(), namespace=VAULT_NAMESPACE),
        )
    assert row is not None
    assert row["namespace"] == VAULT_NAMESPACE
    stored_meta = row["metadata"]
    if isinstance(stored_meta, str):
        stored_meta = json.loads(stored_meta)
    assert stored_meta["secret_redact_fields"]["content"]


class _Root:
    user_id = "root"
    namespace = "default"
    role = "root"
    group_ids: list[str] = []
    authenticated = True
    api_key_id = None


@async_test
async def test_clean_chunk_webhook_is_unchanged(sqlite_backend, monkeypatch):
    """A clean chunk keeps its content and pre-vault namespace (no regression)."""
    clean = "quarterly planning notes, no credentials here"
    payload, status_code, capturing = await _run_import(sqlite_backend, monkeypatch, clean)

    assert status_code == 200, payload
    assert len(capturing.calls) == 1
    data = capturing.calls[0]["payload"]
    assert data["content"] == clean
    assert data["namespace"] == "team"
    assert capturing.calls[0]["namespace"] == "team"


# ---------------------------------------------------------------------------
# Static guard on the call site itself.
#
# The end-to-end tests above prove the payload is correct *today*. This guard
# pins the shape of the `dispatch_event` call so the invariant cannot be
# quietly broken by a later edit to that block: it asserts that `content` is
# passed through the redactor, and that BOTH the payload's "namespace" field
# and the dispatcher's `namespace=` keyword carry `stored_namespace`, never
# the caller's `user.namespace`. That caller value is exactly the pre-vault
# namespace F2 removed -- reintroducing it is the leak.
# ---------------------------------------------------------------------------


def _document_import_tree() -> ast.Module:
    path = Path(__file__).resolve().parents[1] / "mnemos" / "api" / "routes" / "document_import.py"
    return ast.parse(path.read_text(encoding="utf-8"))


def _memory_created_dispatch_call(tree: ast.Module) -> ast.Call:
    """Find the `dispatch_event(..., "memory.created", {...})` call."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "dispatch_event":
            continue
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            if node.args[1].value == "memory.created":
                return node
    raise AssertionError("no dispatch_event(..., 'memory.created', ...) call found")


def _namespace_value(node: ast.AST) -> str | None:
    """Render a namespace expression to its dotted source form."""
    return ast.unparse(node)


def test_memory_created_dispatch_uses_stored_namespace_not_caller_namespace():
    """The outbound `memory.created` payload must describe the row as stored.

    Guards the exact failure F2 fixed: a payload built from `user.namespace`
    (pre-vault) instead of `stored_namespace` (post-classification).
    """
    call = _memory_created_dispatch_call(_document_import_tree())

    payload = call.args[2]
    assert isinstance(payload, ast.Dict), "memory.created payload must be a dict literal"
    fields = {
        key.value: value
        for key, value in zip(payload.keys, payload.values)
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }

    assert "namespace" in fields, "payload is missing the 'namespace' field"
    payload_ns = _namespace_value(fields["namespace"])
    assert payload_ns == "stored_namespace", (
        f"payload['namespace'] is `{payload_ns}`; it must be `stored_namespace` -- "
        "`user.namespace` is the pre-vault namespace and leaks it to the external endpoint"
    )

    # The dispatcher's own namespace kwarg scopes delivery and must agree.
    dispatch_ns = [
        _namespace_value(kw.value)
        for kw in call.keywords
        if kw.arg == "namespace"
    ]
    assert dispatch_ns == ["stored_namespace"], (
        f"dispatch_event(namespace=...) is `{dispatch_ns}`; must be `stored_namespace`"
    )

    # Content must pass through the span redactor with the STORED metadata.
    assert "content" in fields, "payload is missing the 'content' field"
    content = fields["content"]
    assert isinstance(content, ast.Call), "payload['content'] must be a call, not raw content"
    assert _namespace_value(content.func) == "_redacted_for_webhook", (
        "payload['content'] must be wrapped in _redacted_for_webhook()"
    )
    assert _namespace_value(content.args[1]) == "imported.metadata", (
        "the redactor must be driven by the STORED metadata (imported.metadata)"
    )


def test_webhook_fix_precedes_any_caller_namespace_use():
    """A guard against the reviewer-flagged regression: no `user.namespace`
    may reach the dispatch call by any path."""
    call = _memory_created_dispatch_call(_document_import_tree())
    used = {_namespace_value(n) for n in ast.walk(call) if isinstance(n, ast.Attribute)}
    assert "user.namespace" not in used, (
        "the memory.created dispatch call references user.namespace; that is the "
        "pre-vault namespace and reintroduces the F2 credential/namespace leak"
    )
