"""MORPHEUS extraction errors and backend lookup (FB434, tests only).

Pins the retryable-error type and the lazy backend accessor. Divergences from
the brief are pinned and reported; source unchanged.
"""

from __future__ import annotations

import pytest

from mnemos.core import lifecycle
from mnemos.domain.morpheus.runner import MorpheusExtractionError, _get_backend


def test_the_error_is_a_runtime_error() -> None:
    assert issubclass(MorpheusExtractionError, RuntimeError)


def test_the_error_can_be_caught_as_runtime_error() -> None:
    with pytest.raises(RuntimeError):
        raise MorpheusExtractionError("provider returned no triples")


def test_the_error_keeps_its_message() -> None:
    error = MorpheusExtractionError("provider returned no triples")
    assert str(error) == "provider returned no triples"
    assert error.args == ("provider returned no triples",)


def test_the_error_carries_no_context_of_its_own() -> None:
    """DIVERGENCE pinned, not endorsed: the brief says the error carries the
    documented context, but the class is a bare RuntimeError subclass with no
    fields and no custom constructor."""
    error = MorpheusExtractionError("boom")
    assert error.__dict__ == {}
    for name in ("context", "detail", "retryable", "memory_id", "provider"):
        assert not hasattr(error, name), name


def test_the_backend_is_returned_by_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    monkeypatch.setattr(lifecycle, "get_persistence_backend", lambda: sentinel)
    assert _get_backend() is sentinel


def test_the_lookup_is_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider patched after import is still honoured: the import lives
    inside the function, not at module import time."""
    first = object()
    monkeypatch.setattr(lifecycle, "get_persistence_backend", lambda: first)
    assert _get_backend() is first
    second = object()
    monkeypatch.setattr(lifecycle, "get_persistence_backend", lambda: second)
    assert _get_backend() is second


def test_there_is_no_caching(monkeypatch: pytest.MonkeyPatch) -> None:
    """DIVERGENCE pinned, not endorsed: every call asks the lifecycle again;
    two calls return the same object only when the provider itself is stable."""
    sentinel = object()
    calls = []

    def provider() -> object:
        calls.append(1)
        return sentinel

    monkeypatch.setattr(lifecycle, "get_persistence_backend", provider)
    assert _get_backend() is sentinel
    assert _get_backend() is sentinel
    assert len(calls) == 2


def test_unconfigured_raises_an_http_503_not_the_extraction_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """DIVERGENCE pinned, not endorsed: with no backend wired the lifecycle
    accessor raises FastAPI's HTTPException(503). A caller that catches
    MorpheusExtractionError will not see this failure."""
    monkeypatch.setattr(lifecycle, "_persistence_backend", None, raising=True)
    with pytest.raises(Exception) as caught:
        _get_backend()
    error = caught.value
    assert type(error).__name__ == "HTTPException"
    assert getattr(error, "status_code", None) == 503
    assert not isinstance(error, MorpheusExtractionError)


def test_a_provider_failure_propagates_unwrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    boom = RuntimeError("backend registry down")

    def provider() -> object:
        raise boom

    monkeypatch.setattr(lifecycle, "get_persistence_backend", provider)
    with pytest.raises(RuntimeError) as caught:
        _get_backend()
    assert caught.value is boom
