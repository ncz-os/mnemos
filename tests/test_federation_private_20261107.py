"""Characterisation: `_federation_allow_private` (FB772).

Pins the `FEDERATION_ALLOW_PRIVATE` switch: the accepted spellings, the value
used when the switch is absent, and what an unrecognised value actually does.
The settings object is memoised in `config._settings` (there is no
`cache_clear`), so each case sets the env var and clears that global;
`tests/conftest.py::isolate_settings_singleton` already clears it per test.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mnemos.core import config
from mnemos.domain import federation

ENV = "FEDERATION_ALLOW_PRIVATE"
_UNSET = object()


def _evaluate(monkeypatch, raw=_UNSET) -> bool:
    if raw is _UNSET:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, raw)
    monkeypatch.setattr(config, "_settings", None)
    return federation._federation_allow_private()


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "True", "yes", "on", "t", "y"])
def test_accepted_true_spellings(monkeypatch, raw):
    assert _evaluate(monkeypatch, raw) is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "False", "no", "off", "f", "n"])
def test_accepted_false_spellings(monkeypatch, raw):
    assert _evaluate(monkeypatch, raw) is False


def test_an_absent_switch_takes_the_field_default(monkeypatch):
    """The default is `Field(True, ...)`.

    DIVERGENCE: the default is the permissive trusted-LAN posture, not a safe
    one — an operator who never sets the switch silently allows RFC1918 peer
    URLs. The brief's expectation that the absent value is the safe one does
    not hold; the guard that IS unconditional is the metadata-host rejection.
    """
    assert _evaluate(monkeypatch) is True


def test_the_result_is_a_real_bool(monkeypatch):
    assert type(_evaluate(monkeypatch, "false")) is bool
    assert type(_evaluate(monkeypatch, "true")) is bool


def test_the_switch_is_read_per_call(monkeypatch):
    assert _evaluate(monkeypatch, "true") is True
    assert _evaluate(monkeypatch, "false") is False
    assert _evaluate(monkeypatch, "true") is True


def test_the_settings_lookup_is_late_bound(monkeypatch):
    class _Federation:
        allow_private = False

    class _Settings:
        federation = _Federation()

    monkeypatch.setattr(config, "get_settings", lambda: _Settings())
    assert federation._federation_allow_private() is False


@pytest.mark.parametrize("raw", ["banana", "2", "1.0", "-1", ""])
def test_an_unrecognised_value_raises_instead_of_taking_the_default(monkeypatch, raw):
    """DIVERGENCE: an unrecognised value does not fall back to the default — it
    raises `pydantic.ValidationError` out of `_federation_allow_private`, so a
    typo breaks federation on first use rather than being ignored."""
    with pytest.raises(ValidationError):
        _evaluate(monkeypatch, raw)


def test_the_error_names_the_federation_settings_model(monkeypatch):
    with pytest.raises(ValidationError) as exc:
        _evaluate(monkeypatch, "banana")
    assert "_FederationSettings" in str(exc.value)


def test_an_empty_switch_is_not_treated_as_unset(monkeypatch):
    """DIVERGENCE: `FEDERATION_ALLOW_PRIVATE=""` is a common docker-compose
    shape for "unset"; it raises rather than taking the default."""
    with pytest.raises(ValidationError):
        _evaluate(monkeypatch, "")


def test_surrounding_whitespace_is_not_trimmed(monkeypatch):
    """DIVERGENCE: the value is validated verbatim, so a padded `" true "`
    (easy to produce from a shell or compose file) raises."""
    with pytest.raises(ValidationError):
        _evaluate(monkeypatch, " true ")


def test_a_rebuilt_settings_object_reflects_a_changed_environment(monkeypatch):
    first = _evaluate(monkeypatch, "false")
    monkeypatch.setattr(config, "_settings", None)
    monkeypatch.setenv(ENV, "true")
    assert (first, federation._federation_allow_private()) == (False, True)
