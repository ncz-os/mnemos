"""MN-07: classifier coverage for Stripe keys, bearer JWTs, AWS pairs,
split tokens.

Audit evidence (backlog item MN-07, status OPEN): "no stripe/sk_live/eyJ
patterns". Verified against the current source before this suite was
written:

* ``sk_live_...`` / ``sk_test_...`` matched NOTHING. The ``sk_key`` family
  (``mnemos/core/secret_detection.py``) is
  ``\\bsk-(?:proj-|ant-|or-v1-|live-|test-)?[0-9A-Za-z_-]{20,}`` — it
  requires a HYPHEN after ``sk``, but Stripe writes ``sk_live_`` /
  ``rk_live_`` / ``pk_live_`` with an UNDERSCORE, so the whole family
  misses every Stripe key.
* ``eyJ...`` (a base64url JSON header, i.e. any JW*) matched nothing, and
  no detector so far looked at an ``Authorization: Bearer <token>`` value.
* The AWS *access key id* (``AKIA...``) is caught, but the *secret access
  key* only when it sits on an assignment line with a recognised name —
  ``aws_secret_access_key = ...`` / ``AWS_SECRET_ACCESS_KEY=...`` — and
  not in the space-separated pair form the AWS docs and console show.
* A token split across lines/parts ("token: abc123" + "def456" on the
  next line, or a space-separated JWT) was not reassembled, so the
  fragments individually looked too short to be credential-grade.

Every case below must classify as a secret AND must not survive
``redact_content()``; the false-positive guards must stay CLEAN.
"""

from __future__ import annotations

import pytest

from mnemos.core.secret_detection import (
    PREFILTER_SAMPLES,
    SecretClass,
    _prefilter_hits,
    classify,
    redact_content,
    verify_prefilter_superset,
)

# Real-shaped but non-functional credentials (Stripe's and AWS's published
# documentation examples, and JWT fixtures) — never live values.
STRIPE_KEYS = [
    "sk_" "live_51H8xvBKLk3jwFmvNQ2Yz8ZcKQwErTyUiOpAsDfGh",
    "sk_" "test_4eC39HqLyjWDarjtT1zdp7dc",
    "rk_" "live_51H8xvBKLk3jwFmvNQ2Yz8ZcKQwErTyUi",
]

JWT_TOKENS = [
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ."
    "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    "eyJhbGciOiJSUzI1NiIsImtpZCI6ImFiYyJ9.eyJpc3MiOiJtbmVtb3MiLCJhdWQiOiJhcGkifQ."
    "MEUCIQDf7C8x8yHqvW1nYz3Yy8uG6oWQ0J0pF1b2c3d4e5f6g7h8i9j0",
]


@pytest.mark.parametrize("key", STRIPE_KEYS)
def test_stripe_key_vaulted(key):
    finding = classify(key)
    assert finding.cls is SecretClass.VAULT, (key, finding.reasons)
    assert redact_content(key) == "[REDACTED]"


@pytest.mark.parametrize("key", STRIPE_KEYS)
def test_stripe_key_in_prose_redacted(key):
    text = f"stripe key {key} for the billing smoke test"
    finding = classify(text)
    assert finding.cls is not SecretClass.CLEAN, (text, finding.reasons)
    masked = redact_content(text)
    assert key not in masked


@pytest.mark.parametrize("token", JWT_TOKENS)
def test_bearer_jwt_in_header_redacted(token):
    text = f"Authorization: Bearer {token}"
    finding = classify(text)
    assert finding.cls is not SecretClass.CLEAN, (finding.reasons,)
    masked = redact_content(text)
    assert token not in masked
    assert masked.startswith("Authorization: ")


@pytest.mark.parametrize("token", JWT_TOKENS)
def test_bare_jwt_redacted(token):
    finding = classify(token)
    assert finding.cls is not SecretClass.CLEAN, (token, finding.reasons)
    assert token not in redact_content(token)


def test_bearer_jwt_alone_vaults():
    """A memory that IS a bearer token is a credential carrier."""
    finding = classify("Bearer " + JWT_TOKENS[0])
    assert finding.cls is SecretClass.VAULT, finding.reasons


AWS_PAIRS = [
    # space-separated pair, as printed by the AWS IAM console / docs
    "AKIAIOSFODNN7EXAMPLE wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
]


@pytest.mark.parametrize("text", AWS_PAIRS)
def test_aws_access_key_id_and_secret_pair_vaulted(text):
    finding = classify(text)
    assert finding.cls is SecretClass.VAULT, (text, finding.reasons)
    masked = redact_content(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in masked
    assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in masked


def test_aws_secret_access_key_next_to_its_id_redacted():
    text = "creds:\naws_access_key_id = AKIAIOSFODNN7EXAMPLE\naws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    masked = redact_content(text)
    assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in masked


SPLIT_TOKEN_TEXTS = [
    # leading fragment on one line, continuation on the next
    "token: abcdefghijklmn\nopqrstuvwxyz0123456789",
    # two halves of a JWT separated by whitespace
    "jwt eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 eyJzdWIiOiIxMjM0NTY3ODkwIn0 SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    # a Stripe key split after the prefix
    "stripe secret: sk_" "live_51H8xvBKLk3jwFmvNQ2Yz8ZcKQwErTyU iOpAsDfGh",
]


@pytest.mark.parametrize("text", SPLIT_TOKEN_TEXTS)
def test_split_token_is_not_clean(text):
    finding = classify(text)
    assert finding.cls is not SecretClass.CLEAN, (text, finding.reasons)
    masked = redact_content(text)
    for frag in ("opqrstuvwxyz0123456789", "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c", "iOpAsDfGh"):
        if frag in text:
            assert frag not in masked, (text, masked)


# ── false-positive guards: ordinary prose must stay CLEAN ──────────────
CLEAN_PROSE = [
    "the staging build uses sk_test credentials from the vault",
    "we rotate the Stripe secret key every 90 days",
    "JWT tokens are signed with HS256 and expire after one hour",
    "the Authorization header carries a Bearer token",
    "AWS access key ids are prefixed AKIA; the secret never leaves the vault",
    "set the eyJ prefix aside — base64url JSON always starts that way",
    "token refresh happens every 15 minutes",
]


@pytest.mark.parametrize("text", CLEAN_PROSE)
def test_prose_mentions_stay_clean(text):
    finding = classify(text)
    assert finding.cls is SecretClass.CLEAN, (text, finding.reasons)
    assert redact_content(text) == text


def test_new_families_register_prefilter_samples():
    """The fast-path prefilter must stay a true superset (contract)."""
    from mnemos.core.secret_detection import _validated_prefilter_samples

    samples = _validated_prefilter_samples()
    for name in ("stripe_key", "jwt", "aws_secret", "split_token"):
        assert name in PREFILTER_SAMPLES, name
    # Only the aws_secret sample ships reversed (see the contract helper).
    assert samples["aws_secret"] == PREFILTER_SAMPLES["aws_secret"][::-1]
    assert samples["stripe_key"] == PREFILTER_SAMPLES["stripe_key"]
    # Reversal must be harmless for exactly the families the contract is
    # checked on; aws_secret is the one family whose sample ships reversed.
    for name in ("stripe_key", "jwt", "split_token"):
        assert _prefilter_hits(samples[name]), name
    assert verify_prefilter_superset() == []


def test_aws_secret_detector_is_direction_independent():
    """The registered aws_secret sample ships REVERSED (a forward literal
    would publish a credential-shaped string); the detector must fire on
    the real orientation regardless."""
    from mnemos.core.secret_detection import PREFILTER_SAMPLES

    forward = PREFILTER_SAMPLES["aws_secret"][::-1]
    assert forward == "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    finding = classify(forward)
    assert "aws_secret" in finding.reasons, finding.reasons
    assert forward not in redact_content(forward)


# ── known Stripe test-mode documentation credentials ───────────────────
# These are Stripe's own published "testing without your API keys"
# examples. They are syntactically real keys that Stripe rejects outside
# test mode, so token-shape detection must still mask them: an operator
# copy-pasting the docs never expects a credential-looking string to
# survive retrieval, and the corpus above (sk_live_*) already proves the
# live shape.
STRIPE_DOC_TEST_KEYS = [
    "sk_" "test_4eC39HqLyjWDarjtT1zdp7dc",
    "pk_" "test_TYooMQauvdEDq54NiTphI7jx",
]


@pytest.mark.parametrize("key", STRIPE_DOC_TEST_KEYS)
def test_stripe_documentation_test_keys_masked(key):
    text = f"curl https://api.stripe.com -u {key}:"
    masked = redact_content(text)
    assert key not in masked, (key, classify(text).reasons)


def test_commit_sha_is_not_an_aws_secret():
    """A 40-char hex digest carries the AWS secret shape; the sha/commit
    context window that protects the hex-blob detector protects it too."""
    text = "The deploy commit SHA is a1b2c3d4e5f60718293a4b5c6d7e8f90deadbeef in main"
    assert classify(text).cls is SecretClass.CLEAN, classify(text).reasons
    assert redact_content(text) == text


def test_status_id_allowance_never_blanks_an_aws_secret():
    """The FB112 typed-status allowance must refuse a vault-shaped value."""
    import json as _json

    body = _json.dumps({"job_id": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"})
    finding = classify(body)
    assert "aws_secret" in finding.reasons, finding.reasons
    assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in redact_content(body)


def test_split_run_does_not_span_a_dash_or_a_period():
    """A UUID in prose is hyphen-joined; a run joined across "-" or "."
    would swallow ordinary text, so only whitespace joins fragments."""
    uuid_prose = "content 00000000-0000-0000-0000-000000000011"
    assert classify(uuid_prose).cls is SecretClass.CLEAN, classify(uuid_prose).reasons
    assert redact_content(uuid_prose) == uuid_prose
    dotted = "node 10.4 build-2026.11.07 rollout finished"
    assert classify(dotted).cls is SecretClass.CLEAN, classify(dotted).reasons


SPLIT_PROSE_FALSE_POSITIVES = [
    # a bare date is not a wrapped credential
    "payment endpoint 20261017",
    "release notes for 20261107 shipped",
    # word-wrapped sentence whose digits/hex still form a run with a letter
    "The history shows every recovery run was healthy",
    # a sha reference is prose, exactly as for the hex-blob detector
    "the deploy commit SHA is a1b2c3d4e5f60718293a4b5c6d7e8f90deadbeef in main",
]


@pytest.mark.parametrize("text", SPLIT_PROSE_FALSE_POSITIVES)
def test_split_run_does_not_eat_dates_or_sha_prose(text):
    """MN-07 regression: the reassembly path must not swallow prose.

    A run is only a wrapped credential when a fragment is token-grade on its
    own and the joined run is mostly token-grade; a date, a sentence, or a
    sha reference is not, and redacting them corrupts stored content.
    """
    finding = classify(text)
    assert finding.cls is SecretClass.CLEAN, (text, finding.reasons)
    assert redact_content(text) == text
