"""The Postgres-over-fake-connection helper must answer by SQL *intent*.

``FakePostgresConnection`` exists so tests can drive the real Postgres
repository classes. A fake that guesses wrong is worse than no fake at all:
it returns plausible rows for the wrong statement, so the test passes while
proving nothing. These contract tests pin the three registry statements the
audit repository actually emits (verified against
``mnemos/persistence/postgres.py``) and require the fake to reject anything
it does not recognise instead of falling back to "return everything".
"""

from __future__ import annotations

import pytest

from tests._fake_backend import FakePostgresConnection

ROWS = [
    {"provider": "openai", "model_id": "shared"},
    {"provider": "anthropic", "model_id": "shared"},
    {"provider": "groq", "model_id": "llama-3"},
]

# Verbatim statements from PostgresConsultationAuditRepository.
PAIR_SQL = (
    "SELECT provider FROM model_registry "
    "WHERE provider = $1 AND model_id = $2 "
    "  AND available = true AND deprecated = false"
)
MODEL_SQL = (
    "SELECT provider FROM model_registry WHERE model_id = $1   AND available = true AND deprecated = false"
)
DISCOVERY_SQL = """
    SELECT provider, model_id, display_name
    FROM model_registry
    WHERE available = true AND deprecated = false
    ORDER BY graeae_weight DESC NULLS LAST, model_id ASC
"""


def _conn() -> FakePostgresConnection:
    return FakePostgresConnection(registry_rows=ROWS)


def test_pair_lookup_filters_by_provider_not_just_model_id():
    """A model id can be served by several providers; the pair query must
    return only the requested provider's row."""
    got = _conn()._select_registry(PAIR_SQL, ("anthropic", "shared"))
    assert [row["provider"] for row in got] == ["anthropic"]


def test_pair_lookup_is_independent_of_clause_order():
    """The filter must be recognised structurally, so an equivalent
    statement written model-first behaves the same."""
    flipped = (
        "SELECT provider FROM model_registry "
        "WHERE model_id = $1 AND provider = $2 "
        "AND available = true AND deprecated = false"
    )
    # $1 is model_id and $2 is provider in this spelling; the fake must bind
    # each filter to its own placeholder rather than to argument position.
    got = _conn()._select_registry(flipped, ("llama-3", "groq"))
    assert [row["provider"] for row in got] == ["groq"]
    assert _conn()._select_registry(flipped, ("llama-3", "anthropic")) == []


def test_pair_lookup_returns_empty_when_provider_has_no_such_model():
    got = _conn()._select_registry(PAIR_SQL, ("groq", "shared"))
    assert got == []


def test_model_only_lookup_keeps_multi_provider_rows():
    """Discovery-by-id deliberately does not narrow to one provider."""
    got = _conn()._select_registry(MODEL_SQL, ("shared",))
    assert {row["provider"] for row in got} == {"openai", "anthropic"}


def test_discovery_lists_every_available_model():
    got = _conn()._select_registry(DISCOVERY_SQL, ())
    # No weights configured, so NULLS LAST leaves them ordered by model_id.
    assert [row["model_id"] for row in got] == ["llama-3", "shared", "shared"]


def test_unrecognised_statement_is_rejected_not_answered_with_everything():
    """Silently returning the whole registry masks a repository change that
    emits SQL the fake does not model."""
    with pytest.raises(AssertionError, match="unrecognised"):
        _conn()._select_registry("SELECT 1 FROM memory_tags WHERE memory_id = $1", ("m1",))


def test_unrecognised_statement_on_other_table_is_rejected():
    with pytest.raises(AssertionError, match="unrecognised"):
        _conn()._select_registry("SELECT something FROM other_table WHERE x = $1", ("zzz",))
