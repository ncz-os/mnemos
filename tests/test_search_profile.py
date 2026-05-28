"""Unit tests for v6.2 M-2.2.3 retrieval-profile resolver."""

from __future__ import annotations

import pytest

from mnemos.domain.search.profile import SearchProfile, resolve_profile


def test_default_when_none():
    assert resolve_profile(None) is SearchProfile.BALANCED


def test_default_when_empty():
    assert resolve_profile("") is SearchProfile.BALANCED


def test_fast():
    assert resolve_profile("fast") is SearchProfile.FAST


def test_balanced():
    assert resolve_profile("balanced") is SearchProfile.BALANCED


def test_deep():
    assert resolve_profile("deep") is SearchProfile.DEEP


def test_unknown_rejected():
    with pytest.raises(ValueError, match="unknown retrieval profile"):
        resolve_profile("turbo")


def test_case_sensitive():
    # SearchProfile values are lowercase per spec; uppercase rejected.
    with pytest.raises(ValueError):
        resolve_profile("FAST")


def test_custom_default():
    assert resolve_profile(None, default=SearchProfile.FAST) is SearchProfile.FAST


def test_explicit_override_custom_default():
    assert resolve_profile("deep", default=SearchProfile.FAST) is SearchProfile.DEEP
