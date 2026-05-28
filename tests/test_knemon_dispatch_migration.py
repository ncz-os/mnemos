"""Static guards for KNEMON dispatch-plan migration rows."""

from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent

MIGRATIONS = [
    ROOT / "db/migrations/0039_knemon_dispatch_rule_refresh.sql",
    ROOT / "db/migrations_oracle/0039_knemon_dispatch_rule_refresh.sql",
    ROOT / "db/migrations_db2/0039_knemon_dispatch_rule_refresh.sql",
]

EXPECTED_PLAN_NAMES = {
    "claude_max_200",
    "claude_max_100",
    "chatgpt_plus",
    "chatgpt_pro",
    "chatgpt_pro_100",
    "chatgpt_pro_200",
    "codex_plus",
    "codex_pro_100_10x",
    "codex_pro_100_5x",
    "codex_pro_200_25x",
    "codex_pro_200_20x",
}

EXPECTED_ROW_MARKERS = {
    "claude_max_200": ("200", "900", "18000", "2026-05-31"),
    "claude_max_100": ("100", "225", "18000", "2026-06-01"),
    "chatgpt_plus": ("20", "160", "10800", "2026-05-28"),
    "chatgpt_pro": ("200", "unmetered", "2026-05-28"),
    "chatgpt_pro_100": ("100", "unmetered", "2026-05-28"),
    "chatgpt_pro_200": ("200", "unmetered", "2026-05-28"),
    "codex_plus": ("20", "15", "18000", "2026-05-28"),
    "codex_pro_100_10x": ("100", "160", "18000", "2026-05-31", "codex_pro_100"),
    "codex_pro_100_5x": ("100", "80", "18000", "2026-06-01", "codex_pro_100"),
    "codex_pro_200_25x": ("200", "375", "18000", "2026-05-31", "codex_pro_200"),
    "codex_pro_200_20x": ("200", "300", "18000", "2026-06-01", "codex_pro_200"),
}

EXPECTED_CODEX_PRO_PARENTS = {
    "codex_pro_100_10x": "codex_pro_100",
    "codex_pro_100_5x": "codex_pro_100",
    "codex_pro_200_25x": "codex_pro_200",
    "codex_pro_200_20x": "codex_pro_200",
}


def _row_window(sql: str, plan_name: str) -> str:
    idx = sql.index(f"'{plan_name}'")
    return sql[idx : idx + 520]


@pytest.mark.parametrize("migration", MIGRATIONS, ids=lambda path: path.parent.name)
def test_knemon_0039_plan_names_match_across_backends(migration: Path) -> None:
    sql = migration.read_text(encoding="utf-8")

    assert {plan for plan in EXPECTED_PLAN_NAMES if f"'{plan}'" in sql} == EXPECTED_PLAN_NAMES


@pytest.mark.parametrize("migration", MIGRATIONS, ids=lambda path: path.parent.name)
@pytest.mark.parametrize("plan_name, markers", EXPECTED_ROW_MARKERS.items())
def test_knemon_0039_plan_row_markers_match_current_audit(
    migration: Path,
    plan_name: str,
    markers: tuple[str, ...],
) -> None:
    row = _row_window(migration.read_text(encoding="utf-8"), plan_name)

    for marker in markers:
        assert marker in row


@pytest.mark.parametrize("migration", MIGRATIONS, ids=lambda path: path.parent.name)
@pytest.mark.parametrize("plan_name, parent_plan", EXPECTED_CODEX_PRO_PARENTS.items())
def test_knemon_0039_codex_pro_rows_use_stable_parent_aliases(
    migration: Path,
    plan_name: str,
    parent_plan: str,
) -> None:
    row = _row_window(migration.read_text(encoding="utf-8"), plan_name)

    assert f"'interactive', '{parent_plan}'" in row
    assert "'interactive', 'codex_plus'" not in row
