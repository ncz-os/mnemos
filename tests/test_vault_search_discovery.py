"""Vault search/list DISCOVERY semantics (2026-06-19).

Trusted (root) agents must be able to DISCOVER vault-namespace secrets
via search/list -- the rows appear tagged ``vaulted=True`` with content
still redacted -- and then RETRIEVE the value with get_memory(id) /
include_secrets. Non-root callers must never see the vault, even when
they pass the discovery opt-in.

These guard the decoupling of two concerns that used to be one flag:
  * row inclusion (discovery) -> VisibilityFilter.for_read(include_vault=)
  * content unmasking (retrieve) -> _should_redact_secrets / include_secrets
"""

from __future__ import annotations

import mnemos.persistence.visibility as vis
from mnemos.core.secret_detection import VAULT_NAMESPACE
from mnemos.domain.models import row_to_memory
from mnemos.persistence.visibility import DEFAULT_EXCLUDED_NAMESPACES, VisibilityFilter


class _User:
    def __init__(self, user_id: str, *, group_ids=(), root: bool = False) -> None:
        self.user_id = user_id
        self.group_ids = group_ids
        self._root = root


def _patch_is_root(monkeypatch) -> None:
    monkeypatch.setattr(vis, "is_root", lambda u: getattr(u, "_root", False))


def test_root_discovery_includes_vault(monkeypatch) -> None:
    _patch_is_root(monkeypatch)
    f = VisibilityFilter.for_read(_User("default", root=True), namespace=None, include_vault=True)
    assert f.exclude_namespaces == ()


def test_root_default_still_excludes_vault(monkeypatch) -> None:
    _patch_is_root(monkeypatch)
    f = VisibilityFilter.for_read(_User("default", root=True), namespace="default")
    assert f.exclude_namespaces == DEFAULT_EXCLUDED_NAMESPACES


def test_nonroot_discovery_optin_is_ignored(monkeypatch) -> None:
    # A non-root caller passing include_vault MUST still be excluded.
    _patch_is_root(monkeypatch)
    f = VisibilityFilter.for_read(_User("alice"), namespace="alice", include_vault=True)
    assert f.exclude_namespaces == DEFAULT_EXCLUDED_NAMESPACES


def test_root_retrieve_optin_unmasks(monkeypatch) -> None:
    _patch_is_root(monkeypatch)
    f = VisibilityFilter.for_read(
        _User("default", root=True), namespace=None, include_secrets=True
    )
    assert f.exclude_namespaces == ()


def _vault_row() -> dict:
    return {
        "id": "m1",
        "content": "api_token=ABCDEF0123456789",
        "category": "infrastructure",
        "created": "2026-06-19T00:00:00",
        "namespace": VAULT_NAMESPACE,
        "metadata": {"secret_vaulted": True},
    }


def test_vaulted_flag_set_for_vault_namespace_row() -> None:
    item = row_to_memory(_vault_row(), redact_secrets=True)
    assert item.vaulted is True


def test_discovery_row_is_redacted_retrieve_row_is_full() -> None:
    redacted = row_to_memory(_vault_row(), redact_secrets=True)
    full = row_to_memory(_vault_row(), redact_secrets=False)
    assert "[REDACTED]" in redacted.content
    assert "ABCDEF0123456789" not in redacted.content
    assert "ABCDEF0123456789" in full.content


def test_non_vault_row_not_flagged() -> None:
    row = {
        "id": "m2",
        "content": "ordinary note",
        "category": "notes",
        "created": "2026-06-19T00:00:00",
        "namespace": "default",
        "metadata": None,
    }
    assert row_to_memory(row).vaulted is False


def test_secret_vaulted_metadata_flags_even_without_vault_namespace() -> None:
    row = {
        "id": "m3",
        "content": "x",
        "category": "notes",
        "created": "2026-06-19T00:00:00",
        "namespace": "default",
        "metadata": {"secret_vaulted": True},
    }
    assert row_to_memory(row).vaulted is True
