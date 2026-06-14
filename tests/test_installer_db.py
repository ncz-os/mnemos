"""Installer database helper regressions."""

from __future__ import annotations

from pathlib import Path

from mnemos.installer import db
from mnemos.installer.wizard import Config


def test_psql_superuser_file_streams_sql_via_stdin(monkeypatch, tmp_path):
    sql = "SELECT 'migration via stdin';\n"
    migration = tmp_path / "_MEIfake" / "migrations.sql"
    migration.parent.mkdir()
    migration.write_text(sql, encoding="utf-8")

    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return 0, "", ""

    monkeypatch.setattr(db, "_run", fake_run)

    db._psql_superuser_file(str(migration), "mnemos")

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[args.index("-f") + 1] == "-"
    assert str(migration) not in args
    assert kwargs["input"] == sql


def test_selected_migration_groups_scope_server_without_pantheon_or_morpheus():
    cfg = Config(profile="server", selected_components=("server",))

    groups = db.selected_migration_groups(cfg)
    scoped = db.scoped_migration_files(
        [
            Path("migrations.sql"),
            Path("migrations_v3_webhooks.sql"),
            Path("migrations_v3_federation.sql"),
            Path("migrations_v5_2_0_nats_outbox_idempotency.sql"),
            Path("migrations_v4_2_persephone.sql"),
            Path("migrations_v4_2_pantheon_routing_audit.sql"),
            Path("migrations_v4_2_morpheus_consolidate.sql"),
        ],
        groups,
    )
    names = [path.name for path in scoped]

    assert groups == {"core", "webhooks", "federation", "nats", "persephone"}
    assert "migrations.sql" in names
    assert "migrations_v3_webhooks.sql" in names
    assert "migrations_v3_federation.sql" in names
    assert "migrations_v5_2_0_nats_outbox_idempotency.sql" in names
    assert "migrations_v4_2_persephone.sql" in names
    assert "migrations_v4_2_pantheon_routing_audit.sql" not in names
    assert "migrations_v4_2_morpheus_consolidate.sql" not in names


def test_selected_migration_groups_accepts_comma_separated_component_string():
    cfg = Config(profile="server")
    cfg.selected_components = "server,pantheon"  # type: ignore[assignment]

    assert db.selected_migration_groups(cfg) == {
        "core",
        "webhooks",
        "federation",
        "nats",
        "persephone",
        "pantheon",
    }


def test_managed_profile_without_explicit_components_scopes_to_profile_defaults():
    cfg = Config(profile="server", profile_services_enabled=True)

    assert db.selected_migration_components(cfg) == {"server"}
    assert db.selected_migration_groups(cfg) == {
        "core",
        "webhooks",
        "federation",
        "nats",
        "persephone",
    }


def test_run_migrations_applies_only_selected_component_groups(monkeypatch):
    cfg = Config(
        profile="server",
        db_host="localhost",
        db_port=5432,
        db_name="mnemos",
        selected_components=("server",),
    )
    applied: list[str] = []

    def fake_psql_file(path: str, dbname: str):
        applied.append(Path(path).name)
        return 0, "", ""

    monkeypatch.setattr(db, "_psql_superuser_file", fake_psql_file)
    monkeypatch.setattr(
        db,
        "_alter_postgres_embedding_dim",
        lambda config, embedding_dim: True,
    )

    assert db.run_migrations(cfg) is True

    assert "migrations_v3_webhooks.sql" in applied
    assert "migrations_v3_federation.sql" in applied
    assert "migrations_v5_2_0_nats_outbox_idempotency.sql" in applied
    assert "migrations_v4_2_persephone.sql" in applied
    assert "migrations_v4_2_pantheon_routing_audit.sql" not in applied
    assert "migrations_v4_2_morpheus_consolidate.sql" not in applied


def test_run_migrations_managed_profile_skips_unselected_component_groups(monkeypatch):
    cfg = Config(
        profile="server",
        db_host="localhost",
        db_port=5432,
        db_name="mnemos",
        profile_services_enabled=True,
    )
    applied: list[str] = []

    def fake_psql_file(path: str, dbname: str):
        applied.append(Path(path).name)
        return 0, "", ""

    monkeypatch.setattr(db, "_psql_superuser_file", fake_psql_file)
    monkeypatch.setattr(
        db,
        "_alter_postgres_embedding_dim",
        lambda config, embedding_dim: True,
    )

    assert db.run_migrations(cfg) is True

    assert "migrations_v3_webhooks.sql" in applied
    assert "migrations_v3_federation.sql" in applied
    assert "migrations_v5_2_0_nats_outbox_idempotency.sql" in applied
    assert "migrations_v4_2_persephone.sql" in applied
    assert "migrations_v4_2_pantheon_routing_audit.sql" not in applied
    assert "migrations_v4_2_morpheus_consolidate.sql" not in applied


def test_run_migrations_without_selection_preserves_legacy_full_chain(monkeypatch):
    cfg = Config(
        profile="server",
        db_host="localhost",
        db_port=5432,
        db_name="mnemos",
    )
    applied: list[str] = []

    def fake_psql_file(path: str, dbname: str):
        applied.append(Path(path).name)
        return 0, "", ""

    monkeypatch.setattr(db, "_psql_superuser_file", fake_psql_file)
    monkeypatch.setattr(
        db,
        "_alter_postgres_embedding_dim",
        lambda config, embedding_dim: True,
    )

    assert db.run_migrations(cfg) is True

    assert "migrations_v4_2_pantheon_routing_audit.sql" in applied
    assert "migrations_v4_2_morpheus_consolidate.sql" in applied
    assert "0036_hive_agents_subscription_pools.sql" in applied
