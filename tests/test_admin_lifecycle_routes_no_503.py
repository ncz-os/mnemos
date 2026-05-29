from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from mnemos.api.routes import admin
from mnemos.domain.admin_lifecycle_repo import AdminLifecycleRepository


LIFECYCLE_ENDPOINTS = [
    admin.compression_enqueue,
    admin.compression_enqueue_all,
    admin.persephone_sweep,
    admin.persephone_archive_memory,
    admin.persephone_restore_memory,
    admin.persephone_status,
    admin.reload_graeae_providers,
    admin.list_deletion_log,
    admin.create_deletion_request,
    admin.list_deletion_requests,
    admin.get_deletion_request,
    admin.confirm_deletion_request,
    admin.cancel_deletion_request,
    admin.restore_deletion_request,
    admin.force_purge_deletion_request,
]


EXPECTED_LIFECYCLE_ROUTES = {
    (("POST",), "/admin/compression/enqueue"),
    (("POST",), "/admin/compression/enqueue-all"),
    (("POST",), "/admin/persephone/sweep"),
    (("POST",), "/admin/persephone/archive/{memory_id}"),
    (("POST",), "/admin/persephone/restore/{memory_id}"),
    (("GET",), "/admin/persephone/status"),
    (("POST",), "/admin/graeae/reload-providers"),
    (("GET",), "/admin/deletion-log"),
    (("POST",), "/admin/deletion-requests"),
    (("GET",), "/admin/deletion-requests"),
    (("GET",), "/admin/deletion-requests/{request_id}"),
    (("POST",), "/admin/deletion-requests/{request_id}/confirm"),
    (("POST",), "/admin/deletion-requests/{request_id}/cancel"),
    (("POST",), "/admin/deletion-requests/{request_id}/restore"),
    (("POST",), "/admin/deletion-requests/{request_id}/force-purge"),
}


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


def test_admin_lifecycle_route_inventory_is_registered():
    routes = {(tuple(sorted(route.methods or ())), route.path) for route in admin.router.routes}
    assert EXPECTED_LIFECYCLE_ROUTES <= routes


@pytest.mark.parametrize("endpoint", LIFECYCLE_ENDPOINTS)
def test_admin_lifecycle_routes_use_backend_repository_not_postgres_pool(endpoint):
    forbidden_calls = {
        "require_postgres_pool_or_503",
        "_lc.get_pool_manager",
        "_lc._pool",
        "conn.fetch",
        "conn.fetchrow",
        "conn.fetchval",
        "conn.execute",
    }
    calls = _call_names(endpoint)
    assert not (calls & forbidden_calls), endpoint.__name__
    assert isinstance(admin._admin_lifecycle_repo, AdminLifecycleRepository)
