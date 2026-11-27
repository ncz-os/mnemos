"""Characterisation: `_vector_dim_max` (FB811).

The cap on embedding dimensionality is an operator knob, not something a caller
can pass in — and it can only ever raise the floor, never remove it.
"""

from __future__ import annotations

import pytest

from mnemos.persistence import oracle as orc

ENV = "MNEMOS_VECTOR_DIM_MAX"


def test_the_documented_default(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert orc._DEFAULT_VECTOR_DIM_MAX == 4096
    assert orc._vector_dim_max() == 4096


def test_the_result_is_an_int(monkeypatch):
    monkeypatch.setenv(ENV, "8")
    assert type(orc._vector_dim_max()) is int


@pytest.mark.parametrize("raw", ["8", " 12 ", "4096", "1000000"])
def test_an_env_override_is_honoured(monkeypatch, raw):
    monkeypatch.setenv(ENV, raw)
    assert orc._vector_dim_max() == int(raw.strip())


def test_an_underscored_integer_is_still_parsed(monkeypatch):
    """Pinned: the value goes through `int()`, which accepts digit separators,
    so `1_0` is a cap of ten."""
    monkeypatch.setenv(ENV, "1_0")
    assert orc._vector_dim_max() == 10


@pytest.mark.parametrize("raw", ["0", "-5", "abc", "4096.0", "", "  ", "0x10", "None"])
def test_a_bad_override_falls_back_to_the_default(monkeypatch, raw):
    monkeypatch.setenv(ENV, raw)
    assert orc._vector_dim_max() == 4096


@pytest.mark.parametrize("raw", ["0", "-1", "-4096", "junk", "", "1.5", "-0"])
def test_it_never_returns_a_non_positive_number(monkeypatch, raw):
    """Pinned: the floor is enforced in `vector_dim_max_env`, so an operator
    cannot switch the cap off with `0` or a negative value."""
    monkeypatch.setenv(ENV, raw)
    assert orc._vector_dim_max() > 0


def test_the_cap_is_stable_across_calls(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    first, second = orc._vector_dim_max(), orc._vector_dim_max()
    assert first == second == 4096
    monkeypatch.setenv(ENV, "16")
    assert orc._vector_dim_max() == orc._vector_dim_max() == 16


def test_the_environment_is_read_live(monkeypatch):
    monkeypatch.setenv(ENV, "8")
    assert orc._vector_dim_max() == 8
    monkeypatch.setenv(ENV, "9")
    assert orc._vector_dim_max() == 9


def test_it_delegates_to_the_config_helper(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setattr(orc, "vector_dim_max_env", lambda: 7)
    assert orc._vector_dim_max() == 7
