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
    "INFRASTRUCTURE CREDENTIALS: Mac Studio (.10): jasonperlow/DenylistSelfTest@NotARealSecret1",
    "🔑 Credential: ssh mini@192.168.207.66 sudo password = 'mini'",
    "[2026-04-16] SSH ACCESS PATTERNS: root login password is DenylistSelfTest2NotReal99",
    "Use GIT_SSH_COMMAND with sshpass -p 'DenylistSelfTest@NotARealSecret1' to push to ARGONAS",
    "ARGONAS bare repo: root pw DenylistSelfTest@NotARealSecret1 for the git push fallback",
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
    for literal in ("DenylistSelfTest@NotARealSecret1", "DenylistSelfTest2NotReal99", "s3cretDbPass"):
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
    t = "pw DenylistSelfTest@NotARealSecret1 and also DenylistSelfTest2NotReal99 elsewhere"
    masked = redact_content(t)
    assert "DenylistSelfTest@NotARealSecret1" not in masked and "DenylistSelfTest2NotReal99" not in masked
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
    assert redact_content("password=DenylistSelfTest@NotARealSecret1") == "password=[REDACTED]"
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


# ── git branch names in handoff prose are NOT credential records ──────
# False positive: progress notes mentioning branch names such as
# "live/server-snapshot-20261017" were parsed as "user/password" records;
# with multi_span two of them VAULTED the whole memory (mem_1792203811772_2aeb26,
# mem_1792205082552_b1244a).
BRANCH_NAMES = [
    "live/server-snapshot-20261017",
    "fix/api-unknown-agent-409-20261016",
    "fix/workers-reconcile-timer-20261016",
    "feat/phase6-model-router-registry-20261016-clean",
    # unprefixed branch names quoted in notes (hyphen-joined slug on RHS
    # carrying a date-like structural segment — review)
    "alice/server-snapshot-20261017",
    "alice/rollback-v1.2.3",
]

# Unprefixed hyphen-joined names WITHOUT a structural marker are NOT
# exemptible by shape 2 alone: passphrase/diceware credentials use exactly
# that shape (review — that was a security regression), so they
# stay flagged as cred_record_token.
PASSPHRASE_RECORDS = [
    "alice/correct-horse-battery-staple",
    "admin:blue-river-lamp",
]

# The common ref prefix sits on the LEFT of the "/" (the record's "user"
# half), not inside the slug — review finding. Every prefix
# must exempt, including one-segment right halves and short segments.
# Only shape 1 gets this latitude; the unprefixed shape needs a structural
# marker (see PASSPHRASE_RECORDS / NON_BRANCH_SLUGS).
PREFIXED_BRANCH_NAMES = [
    "fix/abc",
    "feat/v1-update",
    "chore/bump-deps",
    "docs/api-v2",
    "live/snapshot",
    "wip/x-y",
    "review/pr-12",
    "release/7-0-5",
    "hotfix/urgent-patch",
    "deploy/prod-20261017",
    "canonical/stamp",
    "audit/lo-gs",
    "refs/heads-ab",
    "origin/main-abc",
    "fix/a-b-c",
]

# real user/password records that MUST stay flagged
REAL_RECORDS = [
    "alice/Tr0ub4dor&3",
    "root:Xk9#mixedCase!7",
    "admin/P@ssw0rd123",
    # structural-marker-free slugs must never be a blanket exemption
    "alice/correct-horse-battery-staple",
    "admin:blue-river-lamp",
]

# unprefixed branch names whose ONLY structural marker is a date / version.
UNPREFIXED_BRANCH_NAMES = [
    # bare slug (no separator): the record must be CLEAN either way
    "server-snapshot-20261017",
    "alice/server-snapshot-20261017",
    "alice/api-unknown-agent-409-20261016",
    "alice/rollback-v1.2.3",
    "alice/rollback-7.0.5-release",
]

# slugs that DO look like passphrases (no date/version/id segment); "409"
# alone is a plain word-ish segment and is not a structural marker.
NON_BRANCH_SLUGS = [
    "correct-horse-battery-staple",
    "blue-river-lamp",
    "horse-battery-staple",
    "server-snapshot-409",
]


@pytest.mark.parametrize(
    "branch", BRANCH_NAMES + PREFIXED_BRANCH_NAMES + UNPREFIXED_BRANCH_NAMES
)
def test_branch_name_is_not_a_credential_record(branch):
    finding = classify(branch)
    assert finding.cls is SecretClass.CLEAN, (branch, finding.reasons)
    assert "cred_record_token" not in finding.reasons
    assert redact_content(branch) == branch


@pytest.mark.parametrize("record", PASSPHRASE_RECORDS)
def test_passphrase_record_is_still_a_credential(record):
    """Shape 2 (unprefixed hyphen-joined slug) must not exempt diceware
    passphrases: no date/version segment means no branch-name marker."""
    finding = classify(record)
    assert finding.cls is SecretClass.REDACT, (record, finding.reasons)
    assert "cred_record_token" in finding.reasons
    masked = redact_content(record)
    assert masked != record
    password = record.replace(":", "/").partition("/")[2]
    assert password not in masked


@pytest.mark.parametrize(
    "user,slug",
    [
        # prefix on the LEFT half exempts even a single-segment, short slug
        ("live", "snapshot"),
        ("fix", "a-b-c"),
        ("feat", "v1-update"),
        ("origin", "main"),
        # a non-prefix user does not make a slug one... unless the slug
        # carries a structural marker (date id / version)
        ("alice", "Tr0ub4dor&3"),
        ("alice", "correct-horse-battery-staple"),
        ("admin", "blue-river-lamp"),
        ("alice", "horse-battery-staple"),
        ("alice", "server-snapshot-409"),
    ],
)
def test_record_helper_sees_the_left_half(user, slug):
    """The git-ref prefix lives in the record's user half, so the helper
    must consult it — checking the slug alone never matches 'live/...'."""
    from mnemos.core.secret_detection import _record_password_is_git_ref

    if user in {"live", "fix", "feat", "origin"}:
        assert _record_password_is_git_ref(user, slug) is True, (user, slug)
    else:
        assert _record_password_is_git_ref(user, slug) is False, (user, slug)


@pytest.mark.parametrize(
    "user,slug",
    [
        ("alice", "server-snapshot-20261017"),
        ("alice", "server-snapshot-12345"),
        ("alice", "rollback-v1.2.3"),
        ("alice", "rollback-7.0.5"),
        ("alice", "api-unknown-agent-409-20261016"),
    ],
)
def test_unprefixed_slug_with_structural_marker_is_a_branch(user, slug):
    """Shape 2 needs a structural marker (>=4-digit id/date or a version)."""
    from mnemos.core.secret_detection import _record_password_is_git_ref

    assert _record_password_is_git_ref(user, slug) is True, (user, slug)


@pytest.mark.parametrize("slug", NON_BRANCH_SLUGS)
def test_unprefixed_passphrase_slug_is_not_a_branch(slug):
    from mnemos.core.secret_detection import _record_password_is_git_ref

    assert _record_password_is_git_ref("alice", slug) is False, slug


@pytest.mark.parametrize("record", REAL_RECORDS)
def test_real_user_password_record_still_flagged(record):
    finding = classify(record)
    assert finding.cls is SecretClass.REDACT, (record, finding.reasons)
    assert "cred_record_token" in finding.reasons
    masked = redact_content(record)
    assert masked != record
    # the password half never survives retrieval
    user, _, password = record.replace(":", "/").partition("/")
    assert password not in masked


def test_branch_names_in_progress_prose_stay_clean():
    """Two branch names in one handoff note must not trip multi_span/VAULT."""
    notes = [
        "Pushed live/server-snapshot-20261017 and "
        "fix/api-unknown-agent-409-20261016, both green on CI.",
        "branches: fix/workers-reconcile-timer-20261016 vs "
        "feat/phase6-model-router-registry-20261016-clean",
        "Handoff: fix/abc-20261016 // live/server-snapshot-20261017 // "
        "feat/phase6-model-router-registry-20261016-clean",
        "Live: canonical/deploy-stamp-20261017 "
        "(see origin/live/server-snapshot-20261017)",
    ]
    for text in notes:
        finding = classify(text)
        assert finding.cls is SecretClass.CLEAN, (text, finding.reasons)
        assert redact_content(text) == text


def test_multiple_real_records_still_vault():
    """The branch-name exemption must not weaken multi_span on real records."""
    finding = classify("creds admin/P@ssw0rd123 and root:Xk9#mixedCase!7")
    assert finding.cls is SecretClass.VAULT, finding.reasons


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

    leak = "root login password is DenylistSelfTest2NotReal99 on TYPHON"
    masked = row_to_memory(_row(leak), redact_secrets=True)
    assert "DenylistSelfTest2NotReal99" not in masked.content
    assert "DenylistSelfTest2NotReal99" not in (masked.verbatim_content or "")


def test_row_to_memory_full_when_not_flagged():
    from mnemos.domain.models import row_to_memory

    leak = "root login password is DenylistSelfTest2NotReal99 on TYPHON"
    full = row_to_memory(_row(leak), redact_secrets=False)
    assert "DenylistSelfTest2NotReal99" in full.content


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


VAULT_CONTENT = "INFRASTRUCTURE CREDENTIALS host TYPHON password DenylistSelfTest@NotARealSecret1"


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
    assert "DenylistSelfTest@NotARealSecret1" not in contents
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
    assert all("DenylistSelfTest@NotARealSecret1" not in r["content"] for r in rows)


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
    assert any("DenylistSelfTest@NotARealSecret1" in r["content"] for r in rows)


@pytest.mark.asyncio
async def test_root_vault_namespace_target_returns_vault(sqlite_backend):
    from mnemos.persistence.visibility import VisibilityFilter

    await _seed(sqlite_backend, content=VAULT_CONTENT, namespace=VAULT_NAMESPACE)
    vis = VisibilityFilter.for_read(_RootUser(), namespace=VAULT_NAMESPACE, include_secrets=False)
    async with sqlite_backend.transactional() as tx:
        rows, _ = await sqlite_backend.memories.list_memories(tx, visibility=vis)
    assert rows and all(r["namespace"] == VAULT_NAMESPACE for r in rows)
    assert any("DenylistSelfTest@NotARealSecret1" in r["content"] for r in rows)


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
    assert all("DenylistSelfTest@NotARealSecret1" not in (r.get("content") or "") for r in rows)


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

    incidental = "Deploy notes: also the root pw DenylistSelfTest@NotARealSecret1 was rotated last week"
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
    assert "DenylistSelfTest@NotARealSecret1" not in (item.content or "")
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


# Credential-shaped records that merely look a little like branch names must
# stay detectable: a letter+digit segment ("hunter2", "x9q") without a date or
# version marker, or a bare 4-digit year in an unprefixed record.
BRANCH_SHAPED_CREDENTIALS = [
    "fix/hunter2-x9q",
    "fix/pass2-v3",
    "alice/hunter2-2024-secret",
]


@pytest.mark.parametrize("record", BRANCH_SHAPED_CREDENTIALS)
def test_branch_shaped_credential_is_not_exempt(record):
    from mnemos.core.secret_detection import _record_password_is_git_ref

    user, _, slug = record.partition("/")
    assert _record_password_is_git_ref(user, slug) is False, record
    finding = classify(record)
    assert finding.cls is SecretClass.REDACT, (record, finding.reasons)
