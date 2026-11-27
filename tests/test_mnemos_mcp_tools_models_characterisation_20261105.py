"""Characterisation tests (2026-11-05) for ``mnemos/mcp/tools/models.py``.

Target: ``mnemos/mcp/tools/models.py`` (254 lines). Its direct importer on
master 782ed312 is ``tests/test_mcp_tools_persistence_abc.py`` (the F-06
slice-2 ABC contract); other suites touch the handlers through the MCP tool
registry rather than importing this module. This file pins the module's own
surface directly.

These tests PIN today's behaviour of the module's public surface (the three
handlers plus the ``TOOLS`` registry entries) and its validation helpers
(the ``_validate_*`` functions and ``_safe_path_segment`` task_type gate),
including normal, edge and error paths. They are not a design proposal: if a
value changes intentionally, update the pin together with the change.

Harness rules:

* No network: every ``_rest_get`` call is replaced by a fake that records
  the call (or asserts it must not happen); the module never opens a socket.
* No live database: the direct-DB path runs against either a real but
  throwaway ``SqliteBackend`` file under ``tmp_path`` (a local file, not a
  server) or an in-process fake backend that answers the ABC call.
* The PANTHEON add-on is absent from this test environment, so the
  ``is_extra_installed("pantheon") == False`` branch (the production
  fallback on a core-only install) is pinned as the *environment's*
  behaviour; the installed-add-on branches run against in-process fake
  ``models_response`` / ``explain_route`` callables (see the
  ``test_pantheon_tools_*`` classes below).

Suspected defects are pinned as ``xfail(strict=True)`` with a comment in
each test; they are listed in the job report. No production code is changed.
"""

from __future__ import annotations

import logging
import sys
from types import ModuleType

import pytest
import pytest_asyncio

from mnemos.core.auth_context import UserContext
from mnemos.core.config import _reset_settings_for_tests
from mnemos.mcp.tools import models as mcp_models
from mnemos.persistence.sqlite import SqliteBackend, _execute

ROOT = UserContext(user_id="root", group_ids=[], role="root", namespace="root", authenticated=True)
ALICE = UserContext(user_id="alice", group_ids=[], role="user", namespace="alice-ns", authenticated=True)

# ``mnemos/mcp/tools/models.py`` is async at its handler surface, but its
# validation helpers and the ``TOOLS`` registry are plain callables. Like
# ``test_mnemos_api_routes_tunnels_characterisation_20261105.py``, there is
# no module-level ``pytestmark = pytest.mark.asyncio``: only async tests are
# marked explicitly, so the sync tests do not trip pytest-asyncio's
# "marked asyncio but not an async function" warning.


class _NoRest:
    """REST fallback must not be reached in this scenario."""

    async def __call__(self, *args, **kwargs):
        raise AssertionError("REST fallback must not be used in this scenario")


class _RecordingRest:
    """Fake ``_rest_get``: records the call and returns a canned payload."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, path, params=None):
        self.calls.append((path, params or {}))
        return self.payload


# ---------------------------------------------------------------------------
# Fake backend plumbing for the direct-DB path
# ---------------------------------------------------------------------------


class _FakeTx:
    """A permissive stand-in for the backend's Transaction handle."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConsultationsAudit:
    """Answers ``fetch_recommended_model`` from a canned tuple/exception."""

    def __init__(self, result=None, error=None):
        self.result = (None, []) if result is None else result
        self.error = error
        self.calls: list[tuple] = []

    async def fetch_recommended_model(self, tx, task_type, cost_budget, quality_floor):
        self.calls.append((task_type, cost_budget, quality_floor))
        if self.error is not None:
            raise self.error
        return self.result


class _FakeBackend:
    """Minimal facade: ``transactional()`` + ``consultations_audit``."""

    def __init__(self, repo=None, *, enter_error=None):
        self.consultations_audit = repo if repo is not None else _FakeConsultationsAudit()
        self.enter_error = enter_error
        self.entered = 0
        self.exited = 0

    def transactional(self):
        return self

    async def __aenter__(self):
        self.entered += 1
        if self.enter_error is not None:
            raise self.enter_error
        return _FakeTx()

    async def __aexit__(self, *exc):
        self.exited += 1
        return False


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path):
    """A real, throwaway SQLite backend with the canonical schema.

    The backend's own migration chain (``open()``) seeds the tables the ABC
    repositories run against; the file lives under ``tmp_path`` (pytest's
    per-test temp dir) and is removed with the test. No server, no network.
    """
    _reset_settings_for_tests()
    from mnemos.core.config import get_settings

    backend = SqliteBackend(str(tmp_path / "models_char_test.db"), get_settings())
    await backend.open()
    yield backend
    await backend.close()


def _install_backend(monkeypatch, backend) -> None:
    """Point ``models._get_persistence_backend`` at the given facade."""
    monkeypatch.setattr(mcp_models, "_get_persistence_backend", lambda: backend)


def _seed_model(backend, **overrides) -> None:
    """Insert one capable row that satisfies the coding policy.

    Defaults: provider ``testprov``/``test-model-1``, capabilities
    ``["code"]`` (canonicalised to ``coding``, the coding policy's required
    cap), input+output cost 2.0+4.0 (avg 3.0 -> tier B), ``graeae_weight``
    0.9 (quality), 128k context. All values sit inside the coding policy's
    allowed tiers (A, B), its 10.0 cost ceiling, and its route policy (no
    reseller/frontier or open-weight-hub violation for this pair).
    """
    row = {
        "provider": "testprov",
        "model_id": "test-model-1",
        "display_name": "Test Model 1",
        "capabilities": '["code"]',
        "input_cost_per_mtok": 2.0,
        "output_cost_per_mtok": 4.0,
        "graeae_weight": 0.9,
        "context_window": 128000,
    }
    row.update(overrides)
    keys = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)

    async def _run():
        async with backend.transactional() as tx:
            await _execute(
                tx.conn,
                f"INSERT INTO model_registry ({keys}) VALUES ({placeholders})",
                tuple(row.values()),
            )

    import asyncio

    asyncio.get_event_loop()  # ensure a loop exists for the sync helper
    return _run()


# ---------------------------------------------------------------------------
# _validate_capabilities
# ---------------------------------------------------------------------------


class TestValidateCapabilities:
    def test_none_passes_through(self):
        assert mcp_models._validate_capabilities(None) is None

    def test_empty_list_is_valid(self):
        assert mcp_models._validate_capabilities([]) == []

    def test_valid_list_is_copied(self):
        caps = ["code", "reasoning", "auto:alias_like"]
        out = mcp_models._validate_capabilities(caps)
        assert out == caps
        assert out is not caps  # a new list, not the caller's object

    def test_boundary_exactly_32(self):
        caps = [f"cap{i}" for i in range(32)]
        assert mcp_models._validate_capabilities(caps) == caps

    def test_over_32_rejected(self):
        caps = [f"cap{i}" for i in range(33)]
        with pytest.raises(ValueError, match="at most 32 items"):
            mcp_models._validate_capabilities(caps)

    @pytest.mark.parametrize("bad", ["code", 42, {"code": True}])
    def test_non_list_rejected(self, bad):
        with pytest.raises(ValueError, match="must be a list with at most 32 items"):
            mcp_models._validate_capabilities(bad)

    @pytest.mark.parametrize(
        "item",
        [42, None, ["code"], "co de", "code!", "code\n", "x" * 65, ""],
    )
    def test_invalid_item_rejected(self, item):
        with pytest.raises(ValueError, match="contains an invalid capability"):
            mcp_models._validate_capabilities([item])

    def test_nul_byte_rejected(self):
        with pytest.raises(ValueError, match="contains an invalid capability"):
            mcp_models._validate_capabilities(["cod\x00e"])


# ---------------------------------------------------------------------------
# _validate_optional_tier
# ---------------------------------------------------------------------------


class TestValidateOptionalTier:
    def test_none_passes_through(self):
        assert mcp_models._validate_optional_tier(None) is None

    @pytest.mark.parametrize("tier", ["budget", "BUDGET", "tier-with:colon_ok", "a" * 64])
    def test_valid_tiers_pass_through(self, tier):
        assert mcp_models._validate_optional_tier(tier) == tier

    @pytest.mark.parametrize("tier", ["", "a" * 65, "with space", "bad!", "x\ny"])
    def test_unsafe_tier_rejected(self, tier):
        with pytest.raises(ValueError, match="filter_tier must be a safe tier string"):
            mcp_models._validate_optional_tier(tier)

    @pytest.mark.parametrize("tier", [42, 3.5, True, ["budget"]])
    def test_non_string_rejected(self, tier):
        with pytest.raises(ValueError, match="filter_tier must be a safe tier string"):
            mcp_models._validate_optional_tier(tier)


# ---------------------------------------------------------------------------
# _validate_max_cost
# ---------------------------------------------------------------------------


class TestValidateMaxCost:
    def test_none_passes_through(self):
        assert mcp_models._validate_max_cost(None) is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0, 0.0), (0.0, 0.0), (5, 5.0), (5.5, 5.5), (100, 100.0)],
    )
    def test_non_negative_numbers_coerce_to_float(self, value, expected):
        result = mcp_models._validate_max_cost(value)
        assert result == expected
        assert isinstance(result, float)

    @pytest.mark.parametrize("value", [-1, -0.5, -1e-9])
    def test_negative_rejected(self, value):
        with pytest.raises(ValueError, match="max_cost must be a non-negative number"):
            mcp_models._validate_max_cost(value)

    @pytest.mark.parametrize("value", [True, False, "5", "1.5", [1], object()])
    def test_non_numeric_rejected(self, value):
        # bool is explicitly excluded even though bool subclasses int.
        with pytest.raises(ValueError, match="max_cost must be a non-negative number"):
            mcp_models._validate_max_cost(value)


# ---------------------------------------------------------------------------
# _validate_model_or_alias
# ---------------------------------------------------------------------------


class TestValidateModelOrAlias:
    @pytest.mark.parametrize("value", ["auto:reasoning", "gpt-5.5", "a", "x" * 256])
    def test_valid_values_strip_and_return(self, value):
        assert mcp_models._validate_model_or_alias(value) == value

    def test_surrounding_whitespace_is_stripped(self):
        assert mcp_models._validate_model_or_alias("  auto:reasoning \n") == "auto:reasoning"

    @pytest.mark.parametrize("value", ["", "   ", "\t", "\n"])
    def test_blank_rejected(self, value):
        with pytest.raises(ValueError, match="model_or_alias must be a non-empty string"):
            mcp_models._validate_model_or_alias(value)

    @pytest.mark.parametrize("value", ["x" * 257, "bell\x07char", "nul\x00byte"])
    def test_control_or_oversize_rejected(self, value):
        with pytest.raises(ValueError, match="model_or_alias must be a non-empty string"):
            mcp_models._validate_model_or_alias(value)

    @pytest.mark.parametrize("value", [None, 42, ["auto"]])
    def test_non_string_rejected(self, value):
        with pytest.raises(ValueError, match="model_or_alias must be a non-empty string"):
            mcp_models._validate_model_or_alias(value)


# ---------------------------------------------------------------------------
# tool_recommend_model: task_type gate
# ---------------------------------------------------------------------------


class TestRecommendModelTaskTypeGate:
    @pytest.mark.parametrize(
        "task_type",
        ["code_generation", "reasoning", "architecture_design", "web_search", "a"],
    )
    def test_safe_task_types_pass_the_gate(self, monkeypatch, task_type):
        monkeypatch.setattr(mcp_models, "_get_persistence_backend", lambda: None)
        rest = _RecordingRest({"recommended": {"cost_per_mtok": 1.0}})
        monkeypatch.setattr(mcp_models, "_rest_get", rest)
        # No exception: the value survives the gate unchanged in the params.
        import asyncio

        asyncio.run(mcp_models.tool_recommend_model(task_type, user=None))
        assert rest.calls[0][1]["task_type"] == task_type

    @pytest.mark.parametrize(
        "task_type",
        ["bad/task", "..", "", "x" * 200, "has space", "quest?ion"],
    )
    def test_unsafe_task_type_raises_before_any_io(self, monkeypatch, task_type):
        monkeypatch.setattr(mcp_models, "_get_persistence_backend", _NoRest())
        monkeypatch.setattr(mcp_models, "_rest_get", _NoRest())
        with pytest.raises(ValueError, match="task_type must be a safe path segment"):
            import asyncio

            asyncio.run(mcp_models.tool_recommend_model(task_type, user=None))

    def test_non_string_task_type_raises_type_error_message(self, monkeypatch):
        monkeypatch.setattr(mcp_models, "_get_persistence_backend", _NoRest())
        with pytest.raises(ValueError, match="task_type must be a string, got int"):
            import asyncio

            asyncio.run(mcp_models.tool_recommend_model(42, user=None))


# ---------------------------------------------------------------------------
# tool_recommend_model: REST fallback (no backend, no user)
# ---------------------------------------------------------------------------


class TestRecommendModelRestFallback:
    @pytest.mark.asyncio
    async def test_rest_request_shape(self, monkeypatch):
        monkeypatch.setattr(mcp_models, "_get_persistence_backend", lambda: None)
        rest = _RecordingRest({"recommended": {"model_id": "m-x", "cost_per_mtok": 2.0}})
        monkeypatch.setattr(mcp_models, "_rest_get", rest)

        result = await mcp_models.tool_recommend_model("code_generation", cost_budget=7.5, quality_floor=0.8, user=None)

        assert rest.calls == [
            (
                "/v1/providers/recommend",
                {"task_type": "code_generation", "cost_budget": 7.5, "quality_floor": 0.8},
            )
        ]
        assert result["success"] is True
        assert result["task_type"] == "code_generation"
        assert result["recommended"] == {"model_id": "m-x", "cost_per_mtok": 2.0}
        assert result["budget_met"] is True  # 2.0 <= 7.5

    @pytest.mark.asyncio
    async def test_response_merges_recommendation_keys(self, monkeypatch):
        monkeypatch.setattr(mcp_models, "_get_persistence_backend", lambda: None)
        rest = _RecordingRest(
            {
                "recommended": {"model_id": "m-x", "cost_per_mtok": 2.0},
                "reasoning": "because",
                "policy": "cost-aware",
            }
        )
        monkeypatch.setattr(mcp_models, "_rest_get", rest)

        result = await mcp_models.tool_recommend_model("reasoning", user=None)

        # The tool's own keys win; recommendation keys merge underneath.
        assert result["reasoning"] == "because"
        assert result["policy"] == "cost-aware"
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_unknown_cost_marks_budget_not_met(self, monkeypatch):
        """``cost_per_mtok`` absent/None (degraded REST fallback) -> False."""
        monkeypatch.setattr(mcp_models, "_get_persistence_backend", lambda: None)
        rest = _RecordingRest({"recommended": {"model_id": "m-x", "cost_per_mtok": None}})
        monkeypatch.setattr(mcp_models, "_rest_get", rest)

        result = await mcp_models.tool_recommend_model("code_generation", cost_budget=10.0, user=None)

        assert result["success"] is True
        assert result["budget_met"] is False

    @pytest.mark.asyncio
    async def test_missing_recommended_key_marks_budget_not_met(self, monkeypatch):
        monkeypatch.setattr(mcp_models, "_get_persistence_backend", lambda: None)
        rest = _RecordingRest({})  # no "recommended" at all
        monkeypatch.setattr(mcp_models, "_rest_get", rest)

        result = await mcp_models.tool_recommend_model("code_generation", cost_budget=10.0, user=None)

        assert result["success"] is True
        assert result["budget_met"] is False

    @pytest.mark.asyncio
    async def test_over_budget_cost_marks_budget_not_met(self, monkeypatch):
        monkeypatch.setattr(mcp_models, "_get_persistence_backend", lambda: None)
        rest = _RecordingRest({"recommended": {"model_id": "m-x", "cost_per_mtok": 99.0}})
        monkeypatch.setattr(mcp_models, "_rest_get", rest)

        result = await mcp_models.tool_recommend_model("code_generation", cost_budget=10.0, user=None)

        assert result["budget_met"] is False

    @pytest.mark.asyncio
    async def test_user_present_without_backend_skips_rest(self, monkeypatch):
        """The REST proxy is gated on ``user is None`` *and* no backend.

        With an authenticated user and no backend the tool reports the
        pre-ABC "Database unavailable" outcome instead of proxying.
        """
        monkeypatch.setattr(mcp_models, "_get_persistence_backend", lambda: None)
        monkeypatch.setattr(mcp_models, "_rest_get", _NoRest())

        result = await mcp_models.tool_recommend_model("code_generation", user=ROOT)

        assert result == {"success": False, "error": "Database unavailable"}


# ---------------------------------------------------------------------------
# tool_recommend_model: direct-DB path (fake backend)
# ---------------------------------------------------------------------------


class TestRecommendModelDirectPath:
    @pytest.mark.asyncio
    async def test_no_models_available(self, monkeypatch):
        backend = _FakeBackend()
        _install_backend(monkeypatch, backend)
        monkeypatch.setattr(mcp_models, "_rest_get", _NoRest())

        result = await mcp_models.tool_recommend_model("code_generation", user=ROOT)

        assert result == {"success": False, "error": "No models available"}
        # The ABC call carried the tool's arguments through.
        assert backend.consultations_audit.calls == [("code_generation", 10.0, 0.7)]

    @pytest.mark.asyncio
    async def test_repo_called_with_positional_args(self, monkeypatch):
        """The tool passes (task_type, cost_budget, quality_floor) positionally."""
        repo = _FakeConsultationsAudit(result=({"model_id": "m", "cost_per_mtok": 1.0}, ["code"]))
        backend = _FakeBackend(repo)
        _install_backend(monkeypatch, backend)

        await mcp_models.tool_recommend_model("reasoning", cost_budget=3.5, quality_floor=0.9, user=ROOT)

        assert repo.calls == [("reasoning", 3.5, 0.9)]

    @pytest.mark.asyncio
    async def test_success_with_priced_model(self, monkeypatch):
        model = {"model_id": "m-x", "cost_per_mtok": 3.0}
        repo = _FakeConsultationsAudit(result=(model, ["code", "reasoning"]))
        backend = _FakeBackend(repo)
        _install_backend(monkeypatch, backend)
        monkeypatch.setattr(mcp_models, "_rest_get", _NoRest())

        result = await mcp_models.tool_recommend_model(
            "code_generation", cost_budget=10.0, quality_floor=0.7, user=ROOT
        )

        assert result == {
            "success": True,
            "task_type": "code_generation",
            "recommended": model,
            "reasoning": "Cheapest model with code, reasoning capability above quality floor 0.7",
            "budget_met": True,  # 3.0 <= 10.0
        }

    @pytest.mark.asyncio
    async def test_unknown_cost_marks_budget_not_met(self, monkeypatch):
        """NULL ``cost_per_mtok`` (degraded fallback row) -> budget_met False.

        Without this the tool would TypeError comparing ``None <= budget``.
        """
        model = {"model_id": "m-x", "cost_per_mtok": None}
        repo = _FakeConsultationsAudit(result=(model, ["code"]))
        _install_backend(monkeypatch, _FakeBackend(repo))

        result = await mcp_models.tool_recommend_model(
            "code_generation", cost_budget=10.0, quality_floor=0.7, user=ROOT
        )

        assert result["success"] is True
        assert result["budget_met"] is False

    @pytest.mark.asyncio
    async def test_over_budget_marks_budget_not_met(self, monkeypatch):
        model = {"model_id": "m-x", "cost_per_mtok": 99.0}
        repo = _FakeConsultationsAudit(result=(model, ["code"]))
        _install_backend(monkeypatch, _FakeBackend(repo))

        result = await mcp_models.tool_recommend_model(
            "code_generation", cost_budget=10.0, quality_floor=0.7, user=ROOT
        )

        assert result["success"] is True
        assert result["budget_met"] is False

    @pytest.mark.asyncio
    async def test_repository_error_is_captured(self, monkeypatch, caplog):
        repo = _FakeConsultationsAudit(error=RuntimeError("relation does not exist"))
        _install_backend(monkeypatch, _FakeBackend(repo))
        monkeypatch.setattr(mcp_models, "_rest_get", _NoRest())
        caplog.set_level(logging.ERROR, logger="mnemos.mcp.tools.models")

        result = await mcp_models.tool_recommend_model("code_generation", user=ROOT)

        assert result == {"success": False, "error": "relation does not exist"}
        assert "[MCP] recommend_model failed" in caplog.text
        assert "relation does not exist" in caplog.text

    @pytest.mark.asyncio
    async def test_user_none_with_live_backend_still_takes_direct_path(self, monkeypatch):
        """The ABC facade replaced the raw-pool guard: ``user is None`` with a
        live backend goes direct, never through REST."""
        backend = _FakeBackend()
        _install_backend(monkeypatch, backend)
        monkeypatch.setattr(mcp_models, "_rest_get", _NoRest())

        result = await mcp_models.tool_recommend_model("code_generation", user=None)

        assert result == {"success": False, "error": "No models available"}
        assert backend.entered == 1


# ---------------------------------------------------------------------------
# tool_recommend_model: direct-DB path (real SQLite backend)
# ---------------------------------------------------------------------------


class TestRecommendModelSqliteBackend:
    @pytest.mark.asyncio
    async def test_recommends_seeded_model(self, sqlite_backend, monkeypatch):
        _install_backend(monkeypatch, sqlite_backend)
        monkeypatch.setattr(mcp_models, "_rest_get", _NoRest())
        async with sqlite_backend.transactional() as tx:
            await _execute(
                tx.conn,
                "INSERT INTO model_registry "
                "(provider, model_id, display_name, capabilities, "
                " input_cost_per_mtok, output_cost_per_mtok, graeae_weight, context_window) "
                "VALUES ('testprov', 'test-model-1', 'Test Model 1', '[\"code\"]', "
                " 2.0, 4.0, 0.9, 128000)",
            )

        result = await mcp_models.tool_recommend_model("code_generation", user=ROOT)

        assert result["success"] is True, result
        assert result["recommended"]["model_id"] == "test-model-1"
        assert result["recommended"]["cost_per_mtok"] == 3.0  # (2.0 + 4.0) / 2
        assert result["reasoning"] == ("Cheapest model with coding capability above quality floor 0.7")
        assert result["budget_met"] is True

    @pytest.mark.asyncio
    async def test_empty_registry_reports_no_models(self, sqlite_backend, monkeypatch):
        _install_backend(monkeypatch, sqlite_backend)
        monkeypatch.setattr(mcp_models, "_rest_get", _NoRest())

        result = await mcp_models.tool_recommend_model("code_generation", user=ROOT)

        assert result == {"success": False, "error": "No models available"}

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "DEFECT: the SQLite schema makes model_registry input/output cost NOT NULL with "
            "default 0, so a NULL-cost degraded row cannot be seeded and this path is "
            "unreachable against the canonical schema (the fake-backend test covers the "
            "repository contract directly)."
        ),
    )
    @pytest.mark.asyncio
    async def test_degraded_row_with_null_cost_marks_budget_not_met(self, sqlite_backend, monkeypatch):
        """The ABC's degraded fallback can return a NULL-cost row; the tool
        must surface ``budget_met=False`` instead of TypeError on
        ``None <= cost_budget``."""
        _install_backend(monkeypatch, sqlite_backend)
        monkeypatch.setattr(mcp_models, "_rest_get", _NoRest())
        async with sqlite_backend.transactional() as tx:
            await _execute(
                tx.conn,
                "INSERT INTO model_registry "
                "(provider, model_id, display_name, capabilities, "
                " input_cost_per_mtok, output_cost_per_mtok, graeae_weight, context_window) "
                "VALUES ('testprov', 'free-model', 'Free Model', '[\"code\"]', "
                " NULL, NULL, 0.9, 128000)",
            )

        result = await mcp_models.tool_recommend_model("code_generation", user=ROOT)

        assert result["success"] is True, result
        assert result["recommended"]["model_id"] == "free-model"
        assert result["recommended"]["cost_per_mtok"] is None
        assert result["budget_met"] is False


# ---------------------------------------------------------------------------
# tool_pantheon_list_models / tool_pantheon_route_explain: extra missing
# ---------------------------------------------------------------------------


class TestPantheonToolsExtraMissing:
    """Environment pin: this checkout has no PANTHEON add-on installed.

    ``is_extra_installed("pantheon")`` is False here (verified at module
    import), so both tools return the documented "not installed" outcome
    without touching validation or a domain import.
    """

    @pytest.mark.asyncio
    async def test_list_models_not_installed(self, monkeypatch):
        monkeypatch.setattr(mcp_models, "is_extra_installed", lambda name: False)
        assert mcp_models.is_extra_installed("pantheon") is False
        result = await mcp_models.tool_pantheon_list_models(filter_capabilities=["code"])
        assert result == {"success": False, "error": "PANTHEON not installed"}

    @pytest.mark.asyncio
    async def test_route_explain_not_installed(self, monkeypatch):
        monkeypatch.setattr(mcp_models, "is_extra_installed", lambda name: False)
        assert mcp_models.is_extra_installed("pantheon") is False
        result = await mcp_models.tool_pantheon_route_explain([{"role": "user", "content": "hi"}], "auto:reasoning")
        assert result == {"success": False, "error": "PANTHEON not installed"}

    @pytest.mark.asyncio
    async def test_not_installed_returns_before_validation(self, monkeypatch):
        """The gate short-circuits: invalid inputs do not raise when the
        extra is absent (validation runs only after the gate)."""
        monkeypatch.setattr(mcp_models, "is_extra_installed", lambda name: False)
        assert mcp_models.is_extra_installed("pantheon") is False
        # Would raise ValueError if validation ran first.
        result = await mcp_models.tool_pantheon_list_models(filter_capabilities=["bad cap!"])
        assert result == {"success": False, "error": "PANTHEON not installed"}


# ---------------------------------------------------------------------------
# tool_pantheon_list_models: installed path (fake domain modules)
# ---------------------------------------------------------------------------


def _install_fake_pantheon(monkeypatch, *, models_response=None, explain_route=None):
    """Provide in-process stand-ins for ``mnemos.domain.pantheon.*``.

    ``tool_pantheon_list_models``/``tool_pantheon_route_explain`` do their
    domain import *after* the extra gate, so installing fake modules in
    ``sys.modules`` and flipping ``is_extra_installed`` to True exercises
    the installed path without the add-on distribution.
    """
    calls = {"models_response": [], "explain_route": []}

    async def _default_models_response(**kwargs):
        calls["models_response"].append(kwargs)
        return {"models": [{"model_id": "fake-model"}], "count": 1}

    async def _default_explain_route(payload):
        calls["explain_route"].append(payload)
        return {"alias": payload["model_or_alias"], "resolved": "fake-resolved"}

    pkg = ModuleType("mnemos.domain.pantheon")
    pkg.__path__ = []  # mark as a package
    catalog = ModuleType("mnemos.domain.pantheon.catalog")
    router = ModuleType("mnemos.domain.pantheon.router")
    catalog.models_response = models_response or _default_models_response
    router.explain_route = explain_route or _default_explain_route
    pkg.catalog = catalog
    pkg.router = router

    monkeypatch.setitem(sys.modules, "mnemos.domain.pantheon", pkg)
    monkeypatch.setitem(sys.modules, "mnemos.domain.pantheon.catalog", catalog)
    monkeypatch.setitem(sys.modules, "mnemos.domain.pantheon.router", router)
    monkeypatch.setattr(mcp_models, "is_extra_installed", lambda name: True)
    return calls


class TestPantheonListModelsInstalled:
    @pytest.mark.asyncio
    async def test_no_filters_passes_none_through(self, monkeypatch):
        calls = _install_fake_pantheon(monkeypatch)

        result = await mcp_models.tool_pantheon_list_models()

        assert result["success"] is True
        assert result["models"] == [{"model_id": "fake-model"}]
        assert result["count"] == 1
        assert calls["models_response"] == [{"filter_capabilities": None, "filter_tier": None, "max_cost": None}]

    @pytest.mark.asyncio
    async def test_filters_are_validated_and_forwarded(self, monkeypatch):
        calls = _install_fake_pantheon(monkeypatch)

        result = await mcp_models.tool_pantheon_list_models(
            filter_capabilities=["code", "reasoning"], filter_tier="budget", max_cost=5.0
        )

        assert result["success"] is True
        assert calls["models_response"] == [
            {
                "filter_capabilities": ["code", "reasoning"],
                "filter_tier": "budget",
                "max_cost": 5.0,
            }
        ]

    @pytest.mark.asyncio
    async def test_user_argument_is_discarded(self, monkeypatch):
        """``del user``: the caller identity never reaches the catalog."""
        calls = _install_fake_pantheon(monkeypatch)

        await mcp_models.tool_pantheon_list_models(user=ROOT)

        assert calls["models_response"][0].keys() == {
            "filter_capabilities",
            "filter_tier",
            "max_cost",
        }

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"filter_capabilities": "not-a-list"}, "must be a list with at most 32 items"),
            ({"filter_capabilities": ["bad cap!"]}, "contains an invalid capability"),
            ({"filter_tier": "bad tier!"}, "filter_tier must be a safe tier string"),
            ({"max_cost": -1}, "max_cost must be a non-negative number"),
            ({"max_cost": "5"}, "max_cost must be a non-negative number"),
        ],
    )
    @pytest.mark.asyncio
    async def test_invalid_filters_raise_value_error(self, monkeypatch, kwargs, match):
        _install_fake_pantheon(monkeypatch)
        with pytest.raises(ValueError, match=match):
            await mcp_models.tool_pantheon_list_models(**kwargs)


# ---------------------------------------------------------------------------
# tool_pantheon_route_explain: installed path (fake domain modules)
# ---------------------------------------------------------------------------


class TestPantheonRouteExplainInstalled:
    @pytest.mark.asyncio
    async def test_valid_input_is_forwarded(self, monkeypatch):
        calls = _install_fake_pantheon(monkeypatch)
        messages = [{"role": "user", "content": "hi"}]

        result = await mcp_models.tool_pantheon_route_explain(messages, "  auto:reasoning  ")

        assert result["success"] is True
        assert result["alias"] == "auto:reasoning"  # stripped by the validator
        assert result["resolved"] == "fake-resolved"
        assert calls["explain_route"] == [{"messages": messages, "model_or_alias": "auto:reasoning"}]

    @pytest.mark.asyncio
    async def test_user_argument_is_discarded(self, monkeypatch):
        calls = _install_fake_pantheon(monkeypatch)
        await mcp_models.tool_pantheon_route_explain([{"role": "user"}], "m", user=ALICE)
        assert calls["explain_route"][0].keys() == {"messages", "model_or_alias"}

    @pytest.mark.parametrize("messages", ["not-a-list", {"role": "user"}, ("m",)])
    @pytest.mark.asyncio
    async def test_non_list_messages_rejected(self, monkeypatch, messages):
        _install_fake_pantheon(monkeypatch)
        with pytest.raises(ValueError, match="messages must be a list with at most 100 items"):
            await mcp_models.tool_pantheon_route_explain(messages, "auto:reasoning")

    @pytest.mark.asyncio
    async def test_over_100_messages_rejected(self, monkeypatch):
        _install_fake_pantheon(monkeypatch)
        messages = [{"role": "user", "content": str(i)} for i in range(101)]
        with pytest.raises(ValueError, match="messages must be a list with at most 100 items"):
            await mcp_models.tool_pantheon_route_explain(messages, "auto:reasoning")

    @pytest.mark.asyncio
    async def test_exactly_100_messages_accepted(self, monkeypatch):
        calls = _install_fake_pantheon(monkeypatch)
        messages = [{"role": "user", "content": str(i)} for i in range(100)]

        result = await mcp_models.tool_pantheon_route_explain(messages, "auto:reasoning")

        assert result["success"] is True
        assert len(calls["explain_route"][0]["messages"]) == 100

    @pytest.mark.parametrize("alias", ["", "   ", "x" * 257, "bad\x00alias"])
    @pytest.mark.asyncio
    async def test_invalid_alias_rejected(self, monkeypatch, alias):
        _install_fake_pantheon(monkeypatch)
        with pytest.raises(ValueError, match="model_or_alias must be a non-empty string"):
            await mcp_models.tool_pantheon_route_explain([{"role": "user"}], alias)

    @pytest.mark.asyncio
    async def test_messages_validation_runs_before_alias_validation(self, monkeypatch):
        """Both are invalid; the messages gate wins (it is checked first)."""
        _install_fake_pantheon(monkeypatch)
        with pytest.raises(ValueError, match="messages must be a list"):
            await mcp_models.tool_pantheon_route_explain("not-a-list", "")


# ---------------------------------------------------------------------------
# TOOLS registry surface
# ---------------------------------------------------------------------------


class TestToolsRegistry:
    def test_expected_tool_names(self):
        assert sorted(mcp_models.TOOLS) == [
            "pantheon_list_models",
            "pantheon_route_explain",
            "recommend_model",
        ]

    @pytest.mark.parametrize("name", ["recommend_model", "pantheon_list_models", "pantheon_route_explain"])
    def test_entry_shape(self, name):
        entry = mcp_models.TOOLS[name]
        assert set(entry) == {"description", "parameters", "required", "handler"}
        assert isinstance(entry["description"], str) and entry["description"]
        assert isinstance(entry["parameters"], dict)
        assert isinstance(entry["required"], list)

    @pytest.mark.parametrize(
        ("name", "required"),
        [
            ("recommend_model", ["task_type"]),
            ("pantheon_list_models", []),
            ("pantheon_route_explain", ["messages", "model_or_alias"]),
        ],
    )
    def test_required_parameters(self, name, required):
        assert mcp_models.TOOLS[name]["required"] == required

    @pytest.mark.parametrize(
        ("name", "handler"),
        [
            ("recommend_model", mcp_models.tool_recommend_model),
            ("pantheon_list_models", mcp_models.tool_pantheon_list_models),
            ("pantheon_route_explain", mcp_models.tool_pantheon_route_explain),
        ],
    )
    def test_handler_binding(self, name, handler):
        assert mcp_models.TOOLS[name]["handler"] is handler

    @pytest.mark.parametrize(
        ("name", "prop", "expected"),
        [
            ("recommend_model", "type", "string"),
            ("recommend_model", "description", "Task type (code_generation, reasoning, architecture_design, etc.)"),
            ("pantheon_list_models", "type", "array"),
            ("pantheon_list_models", "maxItems", 32),
            ("pantheon_route_explain", "type", "array"),
            ("pantheon_route_explain", "maxItems", 100),
        ],
    )
    def test_parameter_schema_values(self, name, prop, expected):
        # The schema for the first parameter of each tool (they are all the
        # first required/only property of interest in this pin).
        params = mcp_models.TOOLS[name]["parameters"]
        if name == "recommend_model":
            target = params["task_type"]
        elif name == "pantheon_list_models":
            target = params["filter_capabilities"]
        else:
            target = params["messages"]
        assert target[prop] == expected

    def test_recommend_model_cost_budget_and_quality_floor_defaults_in_schema(self):
        params = mcp_models.TOOLS["recommend_model"]["parameters"]
        assert params["cost_budget"] == {"type": "number", "description": "Max $/MTok (default: 10.0)"}
        assert params["quality_floor"] == {"type": "number", "description": "Min quality score (default: 0.7)"}

    def test_pantheon_list_models_tier_and_cost_schema(self):
        params = mcp_models.TOOLS["pantheon_list_models"]["parameters"]
        assert params["filter_tier"]["type"] == "string"
        assert params["max_cost"]["type"] == "number"

    def test_pantheon_route_explain_messages_schema(self):
        params = mcp_models.TOOLS["pantheon_route_explain"]["parameters"]
        assert params["messages"]["items"] == {"type": "object"}
        assert params["model_or_alias"]["type"] == "string"


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


class TestModuleSurface:
    def test_public_handlers_are_async(self):
        import inspect

        for handler in (
            mcp_models.tool_recommend_model,
            mcp_models.tool_pantheon_list_models,
            mcp_models.tool_pantheon_route_explain,
        ):
            assert inspect.iscoroutinefunction(handler)

    def test_regex_guards_are_module_level(self):
        assert mcp_models._CAPABILITY_RE.pattern == r"\A[A-Za-z0-9_:-]{1,64}\Z"
        assert mcp_models._MODEL_ALIAS_RE.pattern == r"\A[^\x00-\x1f\x7f]{1,256}\Z"

    def test_logger_name(self):
        assert mcp_models.logger.name == "mnemos.mcp.tools.models"
