"""FB136 (audit M7): the SQLite FTS path must not bind raw user input to MATCH.

Audit M7 (audit-mnemos-agent-status-20261102, finding 7): the SQLite
``fts_search`` used to bind the raw user query to ``memories_fts MATCH ?``,
so FTS5 operators were live (``a OR b`` is an OR, ``x -y`` is AND NOT),
malformed input raised ``fts5 syntax error`` into the generic sqlite3.Error
rescue by luck, and the audit's probe ``job_id job-<hex>`` returned 0 rows
while the Postgres arm's ``plainto_tsquery`` path returned the hit.

The fix binds ``_fts5_plain_query(query)`` instead (mnemos/persistence/
sqlite.py): every whitespace-separated token is phrase-quoted with interior
double quotes doubled, a single trailing ``*`` is kept as the FTS5 prefix
operator, control characters are stripped, and nothing indexable yields
None so the caller takes the LIKE rescue those inputs already ended at.

Known sqlite vs postgres differences (documented in the task report, NOT
fixed — they are tokenizer/parser properties, not defects):
- PG's english parser drops stopwords; FTS5 'porter' has none, so a query
  like ``foo AND bar`` additionally requires the literal word ``and``.
- Punctuation that unicode61 keeps out of phrase adjacency (e.g. the
  phrase ``deploy:cleanly`` requires the two words adjacent), whereas PG
  plainto treats the input as independent terms.
- Prefix ``*`` survives on SQLite; PG plainto has no prefix capability.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

from mnemos.persistence import SqliteBackend
from mnemos.persistence import sqlite as storage
from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope
from tests.test_sqlite_vec_candidate_path import _insert_memory

pytest.importorskip("sqlite_vec")

_SEED_IDS = [f"m{i}" for i in range(1, 7)]


@pytest_asyncio.fixture
async def corpus(tmp_path):
    db = SqliteBackend(tmp_path / "fts.db", SimpleNamespace(database=SimpleNamespace(embedding_dim=3)))
    await db.open()
    try:
        async with db.transactional() as tx:
            await _add(db, tx, "m1", "the deploy job-f3aa1c finished cleanly")
            await _add(db, tx, "m2", "the deploy job-f3bb2d crashed with status")
            await _add(db, tx, "m3", "quota window reset near midnight")
            await _add(db, tx, "m4", "universe scale notes")
            await _add(db, tx, "m5", "engine harness pipeline")
            await _add(db, tx, "m6", "квота сброс слова")
        yield db
    finally:
        await db.close()


async def _add(db, tx, mid, content, owner="alice"):
    await _insert_memory(db, tx, memory_id=mid, content=content, owner_id=owner, namespace=owner)


def _visible(owner="alice"):
    return VisibilityFilter(scope=VisibilityScope.OWN_ONLY, user_id=owner, namespace=owner, group_ids=())


async def _fts(db, query):
    async with db.transactional() as tx:
        return await db.memories.fts_search(tx, query=query, limit=10, visibility=_visible())


def _ids(rows):
    return {r["id"] for r in rows}


# --- sanitiser unit pins ------------------------------------------------------


def test_sanitiser_phrase_quotes_every_token():
    assert storage._fts5_plain_query("a OR b") == '"a" "OR" "b"'


def test_sanitiser_doubles_interior_quotes():
    # Interior quotes are doubled inside the emitted phrase string:
    # FTS5's parser reads """hi""" as the phrase text "hi".
    assert storage._fts5_plain_query('say "hi" now') == '"say" """hi""" "now"'


def test_sanitiser_keeps_single_trailing_prefix():
    assert storage._fts5_plain_query("univ* stars") == '"univ"* "stars"'


def test_sanitiser_strips_control_characters():
    assert storage._fts5_plain_query("de\x00ploy j\x1bob") == '"deploy" "job"'



def test_sanitiser_returns_none_when_nothing_indexable():
    assert storage._fts5_plain_query("") is None
    assert storage._fts5_plain_query("   \t ") is None
    assert storage._fts5_plain_query("* * **") is None
    assert storage._fts5_plain_query(None) is None


def test_sanitiser_quote_only_input_still_emits_a_safe_phrase():
    # A lone '"' does not reduce away: it becomes a phrase containing a
    # quoted quote character, which is valid FTS5 and matches nothing.
    out = storage._fts5_plain_query('"')
    assert out is not None
    assert out.startswith('"')


def test_sanitiser_output_contains_no_bare_fts5_operator_shapes():
    for text in ("a OR b", 'x:"y"', "(a) -b", "NEAR(a, b)", "a NOT b", '*"*"'):
        out = storage._fts5_plain_query(text)
        if out is None:
            continue
        for term in out.split(" "):
            core = term[:-1] if term.endswith("*") else term
            assert core.startswith('"') and core.endswith('"'), f"{text!r} -> {out!r}"


# --- acceptance against the real backend --------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,expected",
    [
        # Plain terms: implicit AND, like the PG plainto path.
        ("deploy job-f3aa1c", {"m1"}),
        # The audit's probe: the hyphen must not act as AND NOT.
        ("job-f3aa1c", {"m1"}),
        # Unbalanced quote must not raise; the surviving "cleanly" phrase hits.
        ('deploy "cleanly', {"m1"}),
        # Parens lose operator meaning; unicode61 splits the phrase into the
        # same word tokens, so a stray bracket decodes to a plain term.
        ("(deploy)", {"m1", "m2"}),
        # A phrase whose separator glue (":") survives inside the quoted
        # string requires word adjacency — different from PG plainto.
        ("deploy:cleanly", set()),
        # NEAR/OR/AND are literal terms, ANDed like every other token.
        ("NEAR deploy", set()),
        ("deploy OR", set()),
        ("deploy crashed", {"m2"}),
        # 'NOT' as a literal word is REQUIRED here (no doc contains "not"):
        # the operator has no special meaning any more, so the AND fails.
        ("engine NOT pipeline", set()),
        # Unicode content is reachable through the same phrase path.
        ("сброс", {"m6"}),
        ("квота сброс", {"m6"}),
    ],
)
async def test_fts_query_shapes(corpus, query, expected):
    rows = await _fts(corpus, query)  # must not raise
    assert _ids(rows) == expected


@pytest.mark.asyncio
async def test_audit_probe_shape_returns_the_hit(corpus):
    """The audit's M7 probe: 'job_id job-<hex>' used to lose the row."""
    async with corpus.transactional() as tx:
        await _add(corpus, tx, "probe", "job_id job-f3aa1c completed")
    rows = await _fts(corpus, "job_id job-f3aa1c")
    assert _ids(rows) == {"probe"}


@pytest.mark.asyncio
async def test_prefix_search_survives(corpus):
    rows = await _fts(corpus, "univ*")
    assert _ids(rows) == {"m4"}
    # Interior or lone stars are neutralised: no crash, no prefix effect
    # beyond the escaped phrase tokens.
    assert await _fts(corpus, "un*iverse") == []
    assert await _fts(corpus, "*") == []


@pytest.mark.asyncio
async def test_empty_and_whitespace_go_to_the_like_rescue(corpus):
    # Empty pattern '%%' matches every owner-visible row, exactly like the
    # Postgres arm's ILIKE rescue.
    assert _ids(await _fts(corpus, "")) == set(_SEED_IDS)
    # A whitespace-only pattern is '%   %' — its normal LIKE semantics:
    # no seed document contains three consecutive spaces, so no rows
    # (also what the PG arm returns for the same input).
    assert await _fts(corpus, "   ") == []


@pytest.mark.asyncio
async def test_limit_is_honored_on_the_sanitised_path(corpus):
    async with corpus.transactional() as tx:
        rows = await corpus.memories.fts_search(tx, query="deploy", limit=1, visibility=_visible())
    assert len(rows) == 1
    assert _ids(rows) <= {"m1", "m2"}


# --- hypothesis property: no string ever raises a MATCH syntax error ----------


def _sanitize_and_match_never_raises():
    """Sync property runnable by hypothesis on hundreds of inputs.

    Uses the migration-built FTS5 table shape directly so the property can
    run fast; the end-to-end backend path is covered by the acceptance
    tests above. Production never binds None to MATCH (it takes the LIKE
    rescue), so only non-None sanitiser output is exercised here.
    """
    pytest.importorskip("hypothesis")
    import sqlite3

    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    # Real-world-ish mix: quotes, brackets, operators, control chars,
    # whitespace, unicode. Sizes kept small to keep the round fast.
    alphabet = (
        '"`( ) [ ] { } ~ ^ * \\ AND OR NOT NEAR " : - + ! # % & | $'
        " \t\n\r\x00\x1b 你好 мир 🌍 abc def123 x*y "
    )
    queries = st.text(alphabet=sorted(set(alphabet)), max_size=40)

    @settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(queries)
    def run(query):
        out = storage._fts5_plain_query(query)
        assert out is None or isinstance(out, str)
        con = sqlite3.connect(":memory:")
        try:
            con.execute(
                "CREATE VIRTUAL TABLE t USING fts5(id UNINDEXED, content, tokenize = 'porter')"
            )
            con.execute(
                "INSERT INTO t VALUES ('1', 'the deploy job-f3aa1c finished cleanly')"
            )
            # Production only ever binds non-None sanitiser output (None
            # takes the LIKE rescue), so only the str case reaches MATCH.
            if out is not None:
                con.execute("SELECT id FROM t WHERE t MATCH ?", (out,))
        finally:
            con.close()

    return run


def test_no_arbitrary_input_raises_fts5_syntax_error():
    _sanitize_and_match_never_raises()()
