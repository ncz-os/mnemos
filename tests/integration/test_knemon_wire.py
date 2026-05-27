"""Integration smoke tests for KNEMON wire: pantheon→providers→ledger."""

import pytest
from unittest.mock import patch, MagicMock


class TestKnemonWire:
    """Verify the pantheon→providers→ledger wiring compiles and runs."""

    def test_import_pantheon_route(self):
        from mnemos.pantheon import route
        assert callable(route)

    def test_import_providers_registry(self):
        from mnemos.providers import registry
        assert hasattr(registry, 'invoke')
        assert callable(registry.invoke)

    def test_import_ledger_record(self):
        from mnemos.ledger import record
        assert callable(record)

    def test_import_llm_call(self):
        from mnemos.llm import call
        assert callable(call)

    def test_pantheon_route_returns_string(self):
        """route(task) should return a string model identifier."""
        from mnemos.pantheon import route
        result = route("Write a hello world function")
        assert isinstance(result, str)
        assert len(result) > 0

    @patch("mnemos.llm.ledger_record")
    @patch("mnemos.llm.provider_registry")
    @patch("mnemos.llm.pantheon_route")
    def test_call_wires_pipeline(self, mock_route, mock_registry, mock_ledger):
        """call() must: route → invoke → record (always)."""
        from mnemos.llm import call

        mock_route.return_value = "test-model"
        mock_registry.invoke.return_value = "Hello, world!"

        result = call("Say hello")

        mock_route.assert_called_once_with("Say hello")
        mock_registry.invoke.assert_called_once_with("test-model", "Say hello")
        mock_ledger.assert_called_once_with(
            model="test-model", task="Say hello", result="Hello, world!"
        )
        assert result == "Hello, world!"

    @patch("mnemos.llm.ledger_record")
    @patch("mnemos.llm.provider_registry")
    @patch("mnemos.llm.pantheon_route")
    def test_call_ledger_on_exception(self, mock_route, mock_registry, mock_ledger):
        """ledger.record must fire even when provider.invoke raises."""
        from mnemos.llm import call

        mock_route.return_value = "bad-model"
        mock_registry.invoke.side_effect = RuntimeError("provider down")

        with pytest.raises(RuntimeError, match="provider down"):
            call("This will fail")

        # Ledger must still be called with result=None
        mock_ledger.assert_called_once_with(
            model="bad-model", task="This will fail", result=None
        )

    @patch("mnemos.ledger._record_async")
    def test_ledger_silent_failure(self, mock_async):
        """ledger.record must never raise, even on DB errors."""
        from mnemos.ledger import record
        mock_async.side_effect = RuntimeError("DB connection lost")
        # Should not raise
        record(model="m", task="t", result="r")

    @patch("mnemos.providers.forward_chat_completion")
    @patch("mnemos.providers.route_model")
    def test_provider_invoke_returns_content(self, mock_route, mock_forward):
        """registry.invoke should extract message content from response."""
        from mnemos.providers import registry
        from mnemos.domain.pantheon.router import RouteDecision

        mock_route.return_value = RouteDecision(
            alias="test-model",
            provider="groq",
            model_id="llama-3.3-70b",
            route_type="exact",
            reason="test",
        )
        mock_forward.return_value = {
            "choices": [{"message": {"content": "42"}}]
        }

        result = registry.invoke("test-model", "What is the answer?")
        assert result == "42"

    @patch("mnemos.providers.forward_chat_completion")
    @patch("mnemos.providers.route_model")
    def test_provider_invoke_error_returns_string(self, mock_route, mock_forward):
        """Even on error, invoke must return a string (not raise)."""
        from mnemos.providers import registry
        from mnemos.domain.pantheon.router import RouteDecision

        mock_route.return_value = RouteDecision(
            alias="bad",
            provider="nonexistent",
            model_id=None,
            route_type="exact",
            reason="test",
        )
        mock_forward.side_effect = RuntimeError("gateway timeout")

        result = registry.invoke("bad", "test")
        assert isinstance(result, str)
        assert "KNEMON" in result or "error" in result.lower() or "failed" in result.lower()
