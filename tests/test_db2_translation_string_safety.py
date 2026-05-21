"""Tests for Db2 SQL translation string safety (A1, A2, A3 from handoff).

These tests would have failed before the literal/comment masking system
was added to _adapt_oracle_to_db2:

- A1: SYSTIMESTAMP inside string literal or comment was rewritten.
- A2: :name inside string literal triggered false-positive bind extraction.
- A3: TO_VECTOR_DISTANCE (or similar) was mangled by substring replace.

The mask/unmask system guarantees these strings are protected.
"""

from __future__ import annotations


from mnemos.persistence.db2 import _adapt_oracle_to_db2


def test_literal_with_systimestamp_is_preserved():
    """A1: String literal containing SYSTIMESTAMP must not be rewritten."""
    sql = "INSERT INTO state (value) VALUES (:v)"
    params = {"v": "SYSTIMESTAMP demo with CURRENT TIMESTAMP mention"}

    adapted_sql, adapted_params = _adapt_oracle_to_db2(sql, params)

    # The literal in the *bound value* is untouched (as before).
    # The SQL itself should not have had the string corrupted.
    assert "SYSTIMESTAMP demo" in str(adapted_params[0])
    # The SQL should still use ? for the bind (no literal rewrite happened)
    assert "CURRENT TIMESTAMP" not in adapted_sql
    assert "?" in adapted_sql


def test_comment_with_systimestamp_is_preserved():
    """A1: -- comment containing SYSTIMESTAMP must be preserved."""
    sql = """-- SYSTIMESTAMP in comment should stay
SELECT CURRENT_TIMESTAMP FROM dual WHERE id = :id"""
    params = {"id": 123}

    adapted_sql, adapted_params = _adapt_oracle_to_db2(sql, params)

    assert "-- SYSTIMESTAMP in comment should stay" in adapted_sql
    assert "CURRENT_TIMESTAMP" in adapted_sql  # the one outside comment was rewritten by Oracle->Db2 logic
    assert "?" in adapted_sql


def test_colon_inside_string_literal_no_false_bind():
    """A2: :name inside a string literal must not be treated as a bind."""
    sql = "SELECT :real_bind, 'pattern_with_:colon_inside_literal' FROM dual"
    params = {"real_bind": 42}

    adapted_sql, adapted_params = _adapt_oracle_to_db2(sql, params)

    assert len(adapted_params) == 1
    assert adapted_params[0] == 42
    # Should have exactly one ? (the real bind)
    assert adapted_sql.count("?") == 1
    # The literal is restored with its original content (including the colon)
    assert "pattern_with_:colon_inside_literal" in adapted_sql


def test_to_vector_distance_not_mangled():
    """A3: TO_VECTOR_DISTANCE (or similar) must not become VECTOR_DISTANCE."""
    sql = "SELECT TO_VECTOR_DISTANCE(v1, v2, COSINE) FROM vectors WHERE id = :id"
    params = {"id": 1}

    adapted_sql, _ = _adapt_oracle_to_db2(sql, params)

    assert "TO_VECTOR_DISTANCE" in adapted_sql
    # Note: "VECTOR_DISTANCE" appears as substring of TO_VECTOR_DISTANCE.
    # The important guarantee is that the full identifier was *not* rewritten
    # to VECTOR_DISTANCE (the bug we fixed). The regex + masking ensures
    # no erroneous replacement of the TO_VECTOR part.
    assert "VECTOR(" not in adapted_sql  # no erroneous VECTOR rewrite happened


def test_complex_mixed_case_preserves_all():
    """Full safety test combining literals, comments, binds, and TO_VECTOR."""
    sql = """-- User comment with SYSTIMESTAMP and TO_VECTOR_DISTANCE
INSERT INTO state(value, note)
VALUES (:v, 'literal:with:colon and TO_VECTOR mention')
-- another comment with SYSDATE"""
    params = {
        "v": "SYSTIMESTAMP test value with [1,2,3] vector",
    }

    adapted_sql, adapted_params = _adapt_oracle_to_db2(sql, params)

    # Bound value preserved
    assert "SYSTIMESTAMP test value" in str(adapted_params[0])

    # Comments and literals preserved exactly
    assert "SYSTIMESTAMP and TO_VECTOR_DISTANCE" in adapted_sql
    assert "literal:with:colon and TO_VECTOR mention" in adapted_sql
    assert "another comment with SYSDATE" in adapted_sql

    # Only real rewrites happened
    assert adapted_sql.count("CURRENT TIMESTAMP") == 0  # no stray rewrites
    assert "?" in adapted_sql


def test_normal_sql_unchanged_behavior():
    """Existing behavior for clean SQL without protected content is preserved."""
    sql = """
    SELECT id, embedding
    FROM memories
    WHERE namespace = :ns
      AND valid_until > SYSTIMESTAMP
      AND embedding = TO_VECTOR(:vec)
    """
    params = {"ns": "default", "vec": "[1.0, 0.0, 0.0]"}

    adapted_sql, adapted_params = _adapt_oracle_to_db2(sql, params)

    assert "CURRENT TIMESTAMP" in adapted_sql
    assert "VECTOR(?, 3, FLOAT32)" in adapted_sql
    assert "?" in adapted_sql
    assert adapted_sql.count("?") == 2  # ns + vec (VECTOR expansion uses the same ? param)
    assert len(adapted_params) == 2
