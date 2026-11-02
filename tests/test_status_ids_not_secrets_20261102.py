"""FB112 (2026-11-02): structured status ids must not be vaulted or redacted.

Audit finding F2 (audit-mnemos-agent-status-20261102): 64-hex head/last_good
SHAs, mixed-case branch names, hash-like job ids and numeric pass tallies in a
structured (JSON) status body are wrongly REDACTed by the secret classifier,
and `KEY=__NAME_API_KEY__` placeholder bodies were wrongly VAULTed — which a
non-root doctor reads as 404.

The classifier keeps its old behavior for real secrets: provider tokens,
connection strings, denylisted literals and high-entropy assignments under
any key (including inside an allowlisted status value) must still match.
"""

from mnemos.core.secret_detection import classify, redact_content, SecretClass

_SHA1 = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"
_SHA2 = "0fedcba9f8e7d6c5b4a39281706f5e4d3c2b1a0918273645fedcba9876543210"
_JOBID = "J-0f5b3e9c8d2a41f7b6e5d4c3b2a1908f"

_FULL_BODY = (
    '{"job_id": "%s", "head_sha": "%s", "last_good_head_sha": "%s", '
    '"branch": "claude/refactor-core-Ab3Kx9c", "verdict": "APPROVE", '
    '"phase": "review", "reviewer_model": "deepseek-flash", '
    '"checks": "tests pass 412/412"}'
) % (_JOBID, _SHA1, _SHA2)


def test_the_audits_full_status_body_is_not_redacted():
    f = classify(_FULL_BODY)
    assert f.cls.name == "CLEAN", f.reasons
    assert redact_content(_FULL_BODY) == _FULL_BODY


def test_a_sha_value_under_a_sha_named_field_is_clean():
    body = '"head_sha": "%s"' % _SHA1
    f = classify(body)
    assert f.cls.name == "CLEAN", f.reasons
    assert "hex64" not in f.reasons


def test_last_good_head_sha_is_underscore_context_too():
    body = '"last_good_head_sha": "%s"' % _SHA2
    f = classify(body)
    assert f.cls.name == "CLEAN", f.reasons


def test_a_mixed_case_branch_name_in_a_status_body_is_not_a_credential_token():
    body = '{"branch": "claude/refactor-core-Ab3Kx9c"}'
    f = classify(body)
    assert f.cls.name == "CLEAN", f.reasons
    # The allowlist is scoped to typed status keys only: the same slug under a
    # non-status key keeps the pre-FB112 behavior.
    other = '{"ref": "claude/refactor-core-Ab3Kx9c"}'
    fo = classify(other)
    assert fo.cls.name == "REDACT" and "cred_record_token" in fo.reasons


def test_numeric_pass_tally_is_not_a_password():
    f = classify("tests pass 412/412")
    assert f.cls.name == "CLEAN", f.reasons


def test_macro_placeholder_env_value_does_not_vault():
    f = classify("KEY=__NAME_API_KEY__")
    assert f.cls.name is not SecretClass.VAULT, f.reasons


def test_real_secrets_are_still_vaulted_beside_status_fields():
    body = (
        '{"job_id": "%s", "head_sha": "%s", '
        '"patch_url": "https://u:secretpass@host/x", '
        '"note": "token: ghp_" + "%s"}'
    )
    body = body % (_JOBID, _SHA1, "aA1" * 12)
    f = classify(body)
    assert f.cls.name == "VAULT", f.reasons


def test_a_real_token_hidden_as_a_job_id_value_is_still_caught():
    body = '"job_id": "ghp_%s"' % ("aA1" * 12)
    f = classify(body)
    assert f.cls.name is not SecretClass.CLEAN, f.reasons


def test_secret_grade_env_assignment_is_still_vaulted():
    f = classify("KEY=Tr0ub4dor&3xyz9")
    assert f.cls.name == "VAULT", f.reasons


def test_hex_value_under_a_non_status_key_is_still_redacted():
    body = '"data": "%s"' % _SHA1
    f = classify(body)
    assert f.cls.name == "REDACT", f.reasons


def test_denied_literals_still_vault_a_status_body():
    # The module ships a self-test denylist digest (documented in source).
    body = '{"job_id": "%s", "note": "DenylistSelfTest@NotARealSecret1"}' % _JOBID
    f = classify(body)
    assert f.cls.name == "VAULT", f.reasons


def test_sha_context_rule_unchanged_for_cold_hex_without_keywords():
    # A 64-hex blob with no sha/hash/commit keyword nearby stays REDACT.
    f = classify("value: " + _SHA1)
    assert f.cls.name == "REDACT" and "hex64" in f.reasons
