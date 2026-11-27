from __future__ import annotations

import asyncio
import ast

import pytest

from mnemos.api.routes.admin import _OAUTH_PROVIDER_UPDATE_COLUMNS
from mnemos.api.routes import admin
from mnemos.core import lifecycle
from mnemos.db import portability_repo
from mnemos.installer.__main__ import _resolved_path_within_base
from mnemos.persistence import federation_journal, postgres
from mnemos.portability.charon import _resolve_bundle_path
from scripts.oracle_to_postgres_copy import qi


def test_oauth_provider_update_columns_match_request_fields():
    from mnemos.domain.models import OAuthProviderUpdateRequest

    assert _OAUTH_PROVIDER_UPDATE_COLUMNS == set(OAuthProviderUpdateRequest.model_fields)
    assert "name" not in _OAUTH_PROVIDER_UPDATE_COLUMNS


def test_oauth_provider_update_rejects_unlisted_model_keys(monkeypatch):
    monkeypatch.setattr(admin, "require_write_allowed", lambda: None)
    monkeypatch.setattr(admin, "require_postgres_pool_or_503", lambda **_kwargs: None)

    class Request:
        def model_dump(self):
            return {"display_name": "Provider", "unlisted": "value"}

    with pytest.raises(admin.HTTPException) as exc:
        asyncio.run(admin.update_oauth_provider("provider", Request(), None))
    assert exc.value.status_code == 422


def test_lifecycle_filter_identifiers_are_fixed_and_bound():
    assert lifecycle._SEARCH_FILTER_COLUMNS == (
        "category",
        "subcategory",
        "source_provider",
        "source_model",
        "source_agent",
        "namespace",
    )
    assert lifecycle._SEARCH_FILTER_COLUMN_ALLOWLIST == frozenset(lifecycle._SEARCH_FILTER_COLUMNS)


@pytest.mark.parametrize("module", [portability_repo, postgres])
def test_sidecar_sql_shapes_accept_literals_and_reject_other_identifiers(module):
    assert module._SIDECAR_COLUMNS_RE.fullmatch("id, memory_id, owner_id")
    assert module._SIDECAR_COLUMNS_RE.fullmatch("id::text AS id, memory_id")
    assert not module._SIDECAR_COLUMNS_RE.fullmatch("unlisted-column")

    async def execute_invalid_table():
        await module._fetch_sidecar(
            None,
            table="unlisted_table",
            columns="id",
            memory_id_column="memory_id",
            memory_ids=[],
            effective_owner=None,
            effective_ns=None,
            bound_to_memories=False,
            hard_limit="12",
        )

    with pytest.raises(ValueError):
        asyncio.run(execute_invalid_table())


def test_sidecar_call_sites_use_allowlisted_literals():
    from pathlib import Path

    assert portability_repo._SIDECAR_TABLES == {"kg_triples", "memory_versions", "memory_compressed_variants"}
    assert postgres._SIDECAR_TABLES == {"kg_triples", "memory_versions", "memory_compressed_variants"}
    for path in (Path("mnemos/db/portability_repo.py"), Path("mnemos/persistence/postgres.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_fetch_sidecar"
        ]
        for call in calls:
            keywords = {keyword.arg: keyword.value for keyword in call.keywords}
            assert isinstance(keywords["table"], ast.Constant)
            assert isinstance(keywords["columns"], ast.Constant)
            assert isinstance(keywords["memory_id_column"], ast.Constant)
            if "order_by" in keywords:
                assert isinstance(keywords["order_by"], ast.Constant)


@pytest.mark.parametrize("module", [portability_repo, postgres])
def test_sidecar_limit_is_integer_and_values_remain_bound(module):
    class Connection:
        sql = None
        params = None

        async def fetch(self, sql, *params):
            self.sql = sql
            self.params = params
            return []

    connection = Connection()

    async def execute():
        await module._fetch_sidecar(
            connection,
            table="kg_triples",
            columns="id, memory_id",
            memory_id_column="memory_id",
            memory_ids=["memory-1"],
            effective_owner="owner-1",
            effective_ns=None,
            bound_to_memories=True,
            hard_limit="4",
        )

    asyncio.run(execute())
    assert connection.sql.endswith("LIMIT 5")
    assert "memory-1" not in connection.sql
    assert connection.params[-1] == "owner-1"


def test_federation_placeholder_count_tracks_identifier_count():
    assert federation_journal._JOURNAL_EMBEDDING_FRAGMENTS == {
        (
            "VEC_ToText(journal_embedding.embedding)",
            " LEFT JOIN memory_embeddings journal_embedding ON journal_embedding.memory_id = m.id",
        ),
        ("FROM_VECTOR(m.embedding)", ""),
    }
    for identifiers in ([], ["a"], ["a", "b", "c"]):
        placeholders = federation_journal._id_placeholders(identifiers)
        assert placeholders.count("?") == len(identifiers)


def test_oracle_identifier_quoting_doubles_quote_characters():
    assert qi('table"name') == '"table""name"'


def test_installer_path_resolver_keeps_paths_under_base(tmp_path):
    base = tmp_path / "service"
    base.mkdir()
    inside = base / "mnemos.env"
    inside.write_text("", encoding="utf-8")
    outside = tmp_path / "outside.env"
    outside.write_text("", encoding="utf-8")
    link = base / "linked.env"
    link.symlink_to(outside)

    assert _resolved_path_within_base(inside, base) == inside.resolve()
    assert _resolved_path_within_base(link, base) is None


def test_charon_bundle_path_resolver_keeps_paths_under_root(tmp_path):
    base = tmp_path / "bundle"
    base.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("", encoding="utf-8")
    (base / "linked.md").symlink_to(outside)

    with pytest.raises(ValueError):
        _resolve_bundle_path(base, "linked.md")


def test_docker_downloads_have_sha256_checks():
    from pathlib import Path

    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "UV_INSTALLER_SHA256" in dockerfile
    assert "MNEMOS_EMBED_MODEL_SHA256" in dockerfile
    assert dockerfile.count("sha256sum -c -") >= 2


def test_requested_workflow_actions_are_full_sha_pinned():
    from pathlib import Path

    paths = (
        ".github/workflows/docs-backend-parity.yml",
        ".github/workflows/mnemosctl-build.yml",
        ".github/workflows/pr-check.yml",
    )
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if "uses:" in line:
                ref = line.split("@", 1)[1].split()[0]
                assert len(ref) == 40


def test_security_workflow_permissions_are_explicit():
    from pathlib import Path

    workflow = Path(".github/workflows/security-scans.yml").read_text(encoding="utf-8")
    assert "permissions:\n  contents: read" in workflow
    assert "permissions:\n      actions: read" in workflow
    assert "permissions:\n      security-events: write" in workflow


def test_release_download_artifact_action_uses_full_sha():
    from pathlib import Path

    workflow = Path(".github/workflows/release-images.yml").read_text(encoding="utf-8")
    action_lines = [line.strip() for line in workflow.splitlines() if "actions/download-artifact@" in line]
    assert len(action_lines) == 1
    assert len(action_lines[0].split("@", 1)[1].split()[0]) == 40
    assert "# v4." in action_lines[0]
