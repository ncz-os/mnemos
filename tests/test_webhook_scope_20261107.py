"""Characterisation: webhook scope validation (FB980)."""

from __future__ import annotations

import inspect

import pytest

from mnemos.persistence.sqlite import _validate_webhook_scope

METHODS = ("list_subscriptions", "get_subscription", "revoke_subscription", "list_deliveries")


@pytest.mark.parametrize("method", METHODS)
def test_a_documented_pair_passes(method):
    assert _validate_webhook_scope("user:1", "ns1", method) is None


@pytest.mark.parametrize("method", METHODS)
def test_the_root_operator_view_passes(method):
    assert _validate_webhook_scope(None, None, method) is None


def test_a_missing_namespace_is_refused_naming_both_fields():
    with pytest.raises(ValueError) as caught:
        _validate_webhook_scope("user:1", None, "list_subscriptions")
    message = str(caught.value)
    assert "owner_id" in message
    assert "namespace" in message
    assert "list_subscriptions" in message


def test_a_missing_owner_is_refused_naming_both_fields():
    with pytest.raises(ValueError) as caught:
        _validate_webhook_scope(None, "ns1", "list_subscriptions")
    message = str(caught.value)
    assert "owner_id" in message
    assert "namespace" in message


def test_the_message_is_the_documented_sentence():
    with pytest.raises(ValueError) as caught:
        _validate_webhook_scope("u", None, "get_subscription")
    assert str(caught.value) == (
        "get_subscription requires both owner_id and namespace to be set, "
        "or both to be None for a root/operator view"
    )


@pytest.mark.parametrize("method", METHODS)
def test_the_refusal_names_the_calling_method(method):
    with pytest.raises(ValueError, match=method):
        _validate_webhook_scope("user:1", None, method)


def test_the_check_is_symmetric():
    for owner, namespace in (("user:1", None), (None, "ns1")):
        with pytest.raises(ValueError):
            _validate_webhook_scope(owner, namespace, "list_deliveries")


def test_the_refusal_is_a_value_error():
    with pytest.raises(ValueError) as caught:
        _validate_webhook_scope(None, "ns1", "x")
    assert not isinstance(caught.value, TypeError)


def test_an_unknown_method_is_not_refused():
    assert _validate_webhook_scope("user:1", "ns1", "not_a_real_method") is None
    assert _validate_webhook_scope(None, None, "not_a_real_method") is None


def test_an_empty_string_owner_counts_as_set():
    assert _validate_webhook_scope("", "", "list_subscriptions") is None
    with pytest.raises(ValueError):
        _validate_webhook_scope("", None, "list_subscriptions")


def test_only_none_identity_matters():
    assert _validate_webhook_scope(0, "ns1", "list_subscriptions") is None
    assert _validate_webhook_scope(False, "ns1", "list_subscriptions") is None


def test_the_validator_returns_none_on_success():
    assert _validate_webhook_scope("u", "n", "m") is None


def test_the_validator_is_synchronous():
    assert not inspect.iscoroutinefunction(_validate_webhook_scope)


def test_the_sqlite_and_mysql_validators_agree():
    mysql = pytest.importorskip("mnemos.persistence.mysql")
    for owner, namespace, method in [
        ("u", "n", "m"),
        (None, None, "m"),
        ("u", None, "m"),
        (None, "n", "m"),
    ]:
        try:
            sqlite_result = _validate_webhook_scope(owner, namespace, method)
        except ValueError as exc:
            sqlite_result = ("error", str(exc))
        try:
            mysql_result = mysql._validate_webhook_scope(owner, namespace, method)
        except ValueError as exc:
            mysql_result = ("error", str(exc))
        assert sqlite_result == mysql_result
