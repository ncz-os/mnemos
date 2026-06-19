"""Credential-PROSE detection + redact-at-retrieval + vault-bypass tests.

Release-blocking (2026-06-13). The secret vault caught token *shapes*
but missed credential *prose*: fleet passwords written as
"sudo password X" / "root login password is X" / "INFRASTRUCTURE
CREDENTIALS: user/pw" surfaced on the default search path.

This suite proves three things:

1. The extended detector classifies credential prose (not just token
   regex) -- VAULT for credential records, REDACT for incidental spans,
   CLEAN for legit prose that merely mentions secret-ish words.
2. Redact-at-retrieval masks credential spans on the default scope in
   ``row_to_memory`` and the route gate ``_should_redact_secrets``.
3. The vault is unreachable on EVERY read path for a non-privileged
   (default) caller -- search (FTS + semantic), list, get-by-id,
   rehydrate predicate, federation feed -- while root explicit access
   (include_secrets / vault namespace / GET-by-id) still returns the
   real credential.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mnemos.core.secret_detection import (
    SecretClass,
    VAULT_NAMESPACE,
    classify,
    redact,
    redact_content,
)


# ── confirmed-leak corpus (the actual strings that surfaced) ───────────
CONFIRMED_LEAKS = [
    "INFRASTRUCTURE CREDENTIALS: Mac Studio (.10): jasonperlow/Gumbo@Kona1b",
    "🔑 Credential: ssh mini@192.168.207.66 sudo password = 'mini'",
    "[2026-04-16] SSH ACCESS PATTERNS: root login password is 10kona22",
    "Use GIT_SSH_COMMAND with sshpass -p 'Gumbo@Kona1b' to push to ARGONAS",
    "ARGONAS bare repo: root pw Gumbo@Kona1b for the git push fallback",
    "DSN: postgres://mnemos:s3cretDbPass@192.168.207.67:1521/ORCLPDB1",
]

# legit prose that mentions secret-ish words but carries NO secret value
FALSE_POSITIVE_GUARD = [
    "password rotation policy requires a 90 day expiry on every host",
    "We discussed the credential management approach with the OSPO team",
    "the password is required before login can proceed",
    "Just a normal memory about RiskyEats FRI surface rendering and shards",
    "The deploy commit SHA is a1b2c3d4e5f60718293a4b5c6d7e8f90deadbeef in main",
    "TODO: store the secret in the vault, value still a placeholder",
    # config/prose where a secret-ish word is followed by a non-secret token
    "the flag pass = true in the build config",
    "login: user is the documented default account",
    "token: enabled in the feature matrix",
    "password prompt appears before the dashboard loads",
    "the credential is optional for read-only access",
    "bearer: required is noted in the API docs",
]


@pytest.mark.parametrize("text", CONFIRMED_LEAKS)
def test_confirmed_leaks_are_vaulted(text):
    """Every confirmed-leak string classifies as VAULT (credential record)."""
    finding = classify(text)
    assert finding.cls is SecretClass.VAULT, (text, finding.reasons)
    assert finding.spans, "must carry at least one span to redact"


@pytest.mark.parametrize("text", CONFIRMED_LEAKS)
def test_confirmed_leaks_are_redacted(text):
    """The literal secret never survives redact_content()."""
    masked = redact_content(text)
    for literal in ("Gumbo@Kona1b", "10kona22", "s3cretDbPass"):
        assert literal not in masked, (text, masked)
    # the password = 'mini' span is masked even though "mini" is short
    if "= 'mini'" in text:
        assert "'mini'" not in masked


@pytest.mark.parametrize("text", FALSE_POSITIVE_GUARD)
def test_false_positives_stay_clean(text):
    """Legit prose mentioning secret-ish words is NOT vaulted/redacted."""
    finding = classify(text)
    assert finding.cls is SecretClass.CLEAN, (text, finding.reasons)
    assert redact_content(text) == text


def test_no_colon_prose_password():
    # A single incidental prose-password span (no credential-record header,
    # no second span) is conservatively REDACT-not-VAULT, but the span is
    # still masked -- the secret value never survives retrieval.
    t = "the sudo password Tr0ub4dor&3 is set on every fleet host"
    f = classify(t)
    assert f.cls in (SecretClass.REDACT, SecretClass.VAULT)
    assert f.spans
    assert "Tr0ub4dor&3" not in redact_content(t)


def test_no_colon_prose_credential_record_vaults():
    # The SAME span inside an explicit credential record -> VAULT.
    t = "INFRASTRUCTURE CREDENTIALS: the sudo password is Tr0ub4dor&3 fleetwide"
    f = classify(t)
    assert f.cls is SecretClass.VAULT
    assert "Tr0ub4dor&3" not in redact_content(t)


def test_password_authentication_directive_redacted():
    f = classify("sshd_config has PasswordAuthentication yes for key-break fallback")
    assert f.cls is SecretClass.REDACT
    assert "PasswordAuthentication" not in redact_content(
        "sshd_config has PasswordAuthentication yes for key-break fallback"
    )


def test_basic_auth_url_vaulted():
    t = "fetch from https://admin:hunter2pass@internal.host/path daily"
    assert classify(t).cls is SecretClass.VAULT
    assert "hunter2pass" not in redact_content(t)


def test_env_style_secret_vaulted():
    t = "set REDIS_AUTH=sup3rl0ngredisauth in /etc/mnemos/env"
    assert classify(t).cls is SecretClass.VAULT
    assert "sup3rl0ngredisauth" not in redact_content(t)


def test_redact_offsets_stable_multi_span():
    t = "pw Gumbo@Kona1b and also 10kona22 elsewhere"
    masked = redact_content(t)
    assert "Gumbo@Kona1b" not in masked and "10kona22" not in masked
    assert masked.count("[REDACTED]") >= 2


def test_assign_does_not_vacuum_next_line():
    """An assignment separator must not pull a value off the NEXT line
    (ngc-review 2026-06-13). 'auth token:\\nabc123' is not a credential."""
    t = "the auth token:\nabc123 on the next line is unrelated"
    assert classify(t).cls is SecretClass.CLEAN
    assert redact_content(t) == t


def test_credential_head_weak_value_masked():
    for t in ("token=abc123", "login: s3cr3t", "api_key=abcd"):
        assert classify(t).cls in (SecretClass.REDACT, SecretClass.VAULT), t
        assert "[REDACTED]" in redact_content(t), t


def test_redact_overlapping_and_oob_spans():
    from mnemos.core.secret_detection import redact

    # fleet literal inside an assignment span -> single [REDACTED]
    assert redact_content("password=Gumbo@Kona1b") == "password=[REDACTED]"
    # out-of-bounds / inverted spans are clamped/dropped, not crashed
    assert redact("abc", [(1, 99), (-5, 2), (3, 1)]) == "[REDACTED]"


def test_clean_content_passthrough_identity():
    t = "no secrets here, just a note"
    assert redact_content(t) == t
    assert redact(t, []) == t


def test_prefilter_is_true_superset_of_every_signal():
    """The classify() fast-path prefilter MUST fire for every detector
    family, or a secret would be silently skipped as CLEAN (ngc-review
    2026-06-13). The canonical per-family sample registry lives in the
    module (``PREFILTER_SAMPLES``) so sample-registration is coupled to
    pattern-registration in ONE place; this test enforces the superset."""
    from mnemos.core.secret_detection import (
        PREFILTER_SAMPLES,
        _prefilter_hits,
        verify_prefilter_superset,
    )

    # Mechanically verified: no registered family escapes the prefilter.
    missed = verify_prefilter_superset()
    assert missed == [], f"prefilter is not a superset; missed families: {missed}"

    # And every family's sample must actually classify as a secret (not
    # CLEAN) -- proving the prefilter sample is a real positive, so the
    # superset check is meaningful (not satisfied by an inert string).
    for name, sample in PREFILTER_SAMPLES.items():
        assert _prefilter_hits(sample), f"prefilter missed {name}: {sample!r}"
        assert classify(sample).cls is not SecretClass.CLEAN, name


def test_quoted_multiword_passphrase_redacted():
    t = 'the password is "correct horse battery staple" everywhere'
    f = classify(t)
    assert f.spans
    masked = redact_content(t)
    assert "correct horse battery staple" not in masked


def test_uri_redaction_preserves_trailing_punctuation():
    t = "see (postgres://u:secretpass@h/db) for details."
    masked = redact_content(t)
    assert "secretpass" not in masked
    assert masked.endswith(") for details.")  # closing paren + sentence intact


# ── row_to_memory redact-at-retrieval gate ────────────────────────────
def _row(content, namespace="default"):
    return {
        "id": "mem_x",
        "content": content,
        "category": "infrastructure",
        "subcategory": None,
        "created": "2026-06-13T00:00:00Z",
        "updated": None,
        "metadata": None,
        "quality_rating": None,
        "compressed_content": None,
        "verbatim_content": content,
        "owner_id": "alice",
        "group_id": None,
        "namespace": namespace,
        "permission_mode": 0,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
        "archived_at": None,
    }


def test_row_to_memory_redacts_when_flagged():
    from mnemos.domain.models import row_to_memory

    leak = "root login password is 10kona22 on TYPHON"
    masked = row_to_memory(_row(leak), redact_secrets=True)
    assert "10kona22" not in masked.content
    assert "10kona22" not in (masked.verbatim_content or "")


def test_row_to_memory_full_when_not_flagged():
    from mnemos.domain.models import row_to_memory

    leak = "root login password is 10kona22 on TYPHON"
    full = row_to_memory(_row(leak), redact_secrets=False)
    assert "10kona22" in full.content


# ── _should_redact_secrets route gate ─────────────────────────────────
def test_should_redact_gate():
    from mnemos.api.routes.memories import _should_redact_secrets

    root = SimpleNamespace(user_id="root", role="root", namespace="default", group_ids=[])
    nonroot = SimpleNamespace(user_id="alice", role="user", namespace="alice-ns", group_ids=[])

    # non-root ALWAYS redacts, even targeting vault / include_secrets
    assert _should_redact_secrets(nonroot) is True
    assert _should_redact_secrets(nonroot, include_secrets=True) is True
    assert _should_redact_secrets(nonroot, namespace=VAULT_NAMESPACE) is True
    # root default path redacts; root explicit opt-in does not
    assert _should_redact_secrets(root) is True
    assert _should_redact_secrets(root, include_secrets=True) is False
    assert _should_redact_secrets(root, namespace=VAULT_NAMESPACE) is False


def test_should_redact_for_row_gate():
    """GET-by-id row gate: root sees a VAULT row full, a non-vault row
    redacted; non-root always redacted (ngc-review 2026-06-13)."""
    from mnemos.api.routes.memories import _should_redact_secrets_for_row

    root = SimpleNamespace(user_id="root", role="root", namespace="default", group_ids=[])
    nonroot = SimpleNamespace(user_id="alice", role="user", namespace="alice-ns", group_ids=[])

    vault_row = {"namespace": VAULT_NAMESPACE}
    plain_row = {"namespace": "default"}

    # root: vault row -> full (False); non-vault row -> redact (True)
    assert _should_redact_secrets_for_row(root, vault_row) is False
    assert _should_redact_secrets_for_row(root, plain_row) is True
    # non-root: always redact
    assert _should_redact_secrets_for_row(nonroot, vault_row) is True
    assert _should_redact_secrets_for_row(nonroot, plain_row) is True


# ── VAULT-BYPASS across read paths (real SqliteBackend) ───────────────
def _embed_dim() -> int:
    import os

    return int(os.environ.get("MNEMOS_EMBEDDING_DIM", "768"))


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path):
    from mnemos.persistence import SqliteBackend

    backend = SqliteBackend(tmp_path / "vault.sqlite3", SimpleNamespace())
    await backend.open()
    try:
        yield backend
    finally:
        await backend.close()


async def _seed(backend, *, content, namespace, owner="alice", perm=4):
    mid = f"mem_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    async with backend.transactional() as tx:
        await backend.memories.insert_memory(
            tx,
            memory_id=mid,
            content=content,
            category="infrastructure",
            subcategory=None,
            metadata_json="{}",
            quality_rating=80,
            owner_id=owner,
            namespace=namespace,
            permission_mode=perm,
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            verbatim_content=content,
            created=now,
            updated=now,
        )
    return mid


VAULT_CONTENT = "INFRASTRUCTURE CREDENTIALS host TYPHON password Gumbo@Kona1b"


@pytest.mark.asyncio
async def test_bypass_list_default_excludes_vault(sqlite_backend):
    from mnemos.persistence.visibility import VisibilityFilter

    await _seed(sqlite_backend, content=VAULT_CONTENT, namespace=VAULT_NAMESPACE)
    await _seed(sqlite_backend, content="ordinary note about FRI", namespace="default")

    # default root list (no include_secrets) -> vault excluded
    vis = VisibilityFilter.for_read(_RootUser(), namespace=None, include_secrets=False)
    async with sqlite_backend.transactional() as tx:
        rows, total = await sqlite_backend.memories.list_memories(tx, visibility=vis)
    contents = " ".join(r["content"] for r in rows)
    assert "Gumbo@Kona1b" not in contents
    assert VAULT_NAMESPACE not in [r["namespace"] for r in rows]


@pytest.mark.asyncio
async def test_bypass_fts_default_excludes_vault(sqlite_backend):
    from mnemos.persistence.visibility import VisibilityFilter

    await _seed(sqlite_backend, content=VAULT_CONTENT, namespace=VAULT_NAMESPACE)
    vis = VisibilityFilter.for_read(_RootUser(), namespace=None, include_secrets=False)
    async with sqlite_backend.transactional() as tx:
        rows = await sqlite_backend.memories.fts_search(
            tx, query="password", limit=50, visibility=vis
        )
    assert all(r["namespace"] != VAULT_NAMESPACE for r in rows)
    assert all("Gumbo@Kona1b" not in r["content"] for r in rows)


@pytest.mark.asyncio
async def test_bypass_semantic_default_excludes_vault(sqlite_backend):
    from mnemos.persistence.visibility import VisibilityFilter

    await _seed(sqlite_backend, content=VAULT_CONTENT, namespace=VAULT_NAMESPACE)
    vis = VisibilityFilter.for_read(_RootUser(), namespace=None, include_secrets=False)
    async with sqlite_backend.transactional() as tx:
        rows = await sqlite_backend.memories.semantic_search(
            tx, embedding=[0.0] * _embed_dim(), limit=50, visibility=vis
        )
    assert all(r["namespace"] != VAULT_NAMESPACE for r in rows)


@pytest.mark.asyncio
async def test_root_include_secrets_returns_vault(sqlite_backend):
    from mnemos.persistence.visibility import VisibilityFilter

    await _seed(sqlite_backend, content=VAULT_CONTENT, namespace=VAULT_NAMESPACE)
    # root explicit opt-in -> vault visible, full content
    vis = VisibilityFilter.for_read(_RootUser(), namespace=None, include_secrets=True)
    async with sqlite_backend.transactional() as tx:
        rows, _ = await sqlite_backend.memories.list_memories(tx, visibility=vis)
    assert any(r["namespace"] == VAULT_NAMESPACE for r in rows)
    assert any("Gumbo@Kona1b" in r["content"] for r in rows)


@pytest.mark.asyncio
async def test_root_vault_namespace_target_returns_vault(sqlite_backend):
    from mnemos.persistence.visibility import VisibilityFilter

    await _seed(sqlite_backend, content=VAULT_CONTENT, namespace=VAULT_NAMESPACE)
    vis = VisibilityFilter.for_read(_RootUser(), namespace=VAULT_NAMESPACE, include_secrets=False)
    async with sqlite_backend.transactional() as tx:
        rows, _ = await sqlite_backend.memories.list_memories(tx, visibility=vis)
    assert rows and all(r["namespace"] == VAULT_NAMESPACE for r in rows)
    assert any("Gumbo@Kona1b" in r["content"] for r in rows)


@pytest.mark.asyncio
async def test_nonroot_cannot_target_vault(sqlite_backend):
    """A non-root caller naming namespace=vault still gets the vault subtracted."""
    from mnemos.persistence.visibility import VisibilityFilter

    await _seed(sqlite_backend, content=VAULT_CONTENT, namespace=VAULT_NAMESPACE, owner="alice")
    vis = VisibilityFilter.for_read(_AliceUser(), namespace=VAULT_NAMESPACE, include_secrets=True)
    async with sqlite_backend.transactional() as tx:
        rows, _ = await sqlite_backend.memories.list_memories(tx, visibility=vis)
    assert rows == []  # non-root never enumerates the vault, even naming it


@pytest.mark.asyncio
async def test_bypass_federation_feed_excludes_vault(sqlite_backend):
    """federation feed_query never serves a vaulted memory."""
    await _seed(sqlite_backend, content=VAULT_CONTENT, namespace=VAULT_NAMESPACE, perm=7)
    await _seed(sqlite_backend, content="world readable note", namespace="default", perm=7)
    async with sqlite_backend.transactional() as tx:
        rows = await sqlite_backend.federation.feed_query(
            tx,
            since_updated=None,
            since_id=None,
            namespaces=[],
            categories=[],
            limit=100,
            prefer_compressed=False,
        )
    assert all(r.get("namespace") != VAULT_NAMESPACE for r in rows)
    assert all("Gumbo@Kona1b" not in (r.get("content") or "") for r in rows)


@pytest.mark.asyncio
async def test_bypass_federation_feed_explicit_vault_namespace(sqlite_backend):
    """Even an explicit namespaces=['vault'] filter cannot pull vault rows."""
    await _seed(sqlite_backend, content=VAULT_CONTENT, namespace=VAULT_NAMESPACE, perm=7)
    async with sqlite_backend.transactional() as tx:
        rows = await sqlite_backend.federation.feed_query(
            tx,
            since_updated=None,
            since_id=None,
            namespaces=[VAULT_NAMESPACE],
            categories=[],
            limit=100,
            prefer_compressed=False,
        )
    assert rows == []


@pytest.mark.asyncio
async def test_bypass_get_feed_memory_excludes_vault(sqlite_backend):
    mid = await _seed(sqlite_backend, content=VAULT_CONTENT, namespace=VAULT_NAMESPACE, perm=7)
    async with sqlite_backend.transactional() as tx:
        row = await sqlite_backend.federation.get_feed_memory(
            tx, mid, namespaces=[], categories=[]
        )
    assert row is None


@pytest.mark.asyncio
async def test_federation_item_redacts_incidental_span(sqlite_backend):
    """A world-readable, NON-vaulted memory with an incidental credential
    span is masked before it crosses the federation feed."""
    from mnemos.api.routes.federation import _memory_item_from_row

    incidental = "Deploy notes: also the root pw Gumbo@Kona1b was rotated last week"
    mid = await _seed(sqlite_backend, content=incidental, namespace="default", perm=7)
    async with sqlite_backend.transactional() as tx:
        rows = await sqlite_backend.federation.feed_query(
            tx,
            since_updated=None,
            since_id=None,
            namespaces=[],
            categories=[],
            limit=100,
            prefer_compressed=False,
        )
    target = [r for r in rows if r["id"] == mid]
    assert target, "incidental-span memory should still federate (not vaulted)"
    item = _memory_item_from_row(target[0])
    assert "Gumbo@Kona1b" not in (item.content or "")
    assert "[REDACTED]" in item.content


# ── lightweight user stand-ins (avoid importing UserContext shape) ─────
class _RootUser:
    user_id = "root"
    namespace = "default"
    role = "root"
    group_ids: list = []


class _AliceUser:
    user_id = "alice"
    namespace = "alice-ns"
    role = "user"
    group_ids: list = []
