from __future__ import annotations

import ast
import inspect
import textwrap

from mnemos.api.routes import document_import
from mnemos.db.document_repo import DocumentRepository


def _call_names(func) -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    names: set[str] = set()

    def dotted(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = dotted(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = dotted(node.func)
            if name:
                names.add(name)
    return names


def test_document_import_routes_are_registered():
    routes = {
        (tuple(sorted(route.methods or ())), route.path)
        for route in document_import.router.routes
    }
    assert (("POST",), "/v1/documents/import") in routes
    assert (("POST",), "/v1/documents/batch-import") in routes


def test_document_import_routes_use_backend_repository_not_postgres_pool():
    handlers = [
        document_import.import_memories_from_document,
        document_import.import_document,
        document_import.batch_import_documents,
    ]
    forbidden_calls = {
        "require_postgres_pool_or_503",
        "_lc.get_pool_manager",
        "_lc._pool",
        "conn.fetch",
        "conn.fetchrow",
        "conn.fetchval",
        "conn.execute",
    }
    for handler in handlers:
        calls = _call_names(handler)
        assert not (calls & forbidden_calls), handler.__name__

    assert isinstance(document_import._document_repo, DocumentRepository)
