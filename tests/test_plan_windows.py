from __future__ import annotations

from datetime import datetime, timezone

from mnemos.core.plan_windows import compute_plan_window_id, plan_path_kind


def test_chatgpt_pro_variants_use_monthly_unmetered_windows() -> None:
    ts = datetime(2026, 5, 28, 18, 30, tzinfo=timezone.utc)

    for plan_name in ("chatgpt_pro", "chatgpt_pro_100", "chatgpt_pro_200"):
        assert compute_plan_window_id("openai", plan_name, ts) == f"openai-{plan_name}-2026-05"
        assert plan_path_kind("openai", plan_name) == "unmetered"


def test_explicit_path_kind_overrides_known_plan_default() -> None:
    assert plan_path_kind("openai", "chatgpt_plus", "api") == "api"
