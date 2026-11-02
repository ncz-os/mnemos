# PLAN-FB112 — status ids must not be vaulted or redacted (audit F2)

## Premise (re-verified at master dd74b5f2)
`mnemos/core/secret_detection.py:classify()` on a structured JSON status body returns
REDACT with bogus spans. Probe results against head:
- `"head_sha": "<64hex>"` → REDACT span `hex64` — the sha-context window check
  (`_SHA_CONTEXT_RE = \b(sha|commit|…)\b`) cannot match inside `head_sha` because `_`
  and `s` are both word chars, so `\bsha\b` never fires before the value
  (`last_good_head_sha` likewise; 2+ hex values compound the damage).
- `"branch": "claude/refactor-core-Ab3Kx9c"` → REDACT span `cred_record_token`
  (right half is not recognized as a slug).
- `tests pass 412/412` → REDACT span `prose_pass` (bare token "412/412") — numeric
  tally is not a password.
- `KEY=__NAME_API_KEY__` → VAULT `env_secret` (`__NAME_API_KEY__` is not in
  `_PLACEHOLDER_RE`, so the placeholder body vaults → 404 for a non-root doctor).
- `"job_id": "<uuid4>"` → CLEAN (fine).

## Fix (minimal, per the audit's proposals)
All inside `mnemos/core/secret_detection.py`, no API/schema change:
1. **Typed-field allowlist for structured JSON bodies.** New `_status_exempt_spans(text)`:
   only when the WHOLE content parses as one JSON object, collect top-level (+nested
   dict) leaf string values under allowlisted status keys (`job_id`, `*_sha`/`sha`,
   `branch`, `verdict`, `phase`, `reviewer_model`) whose value matches a STRICT type
   regex (40/64-hex or uuid for shas/job_id; bounded id slug for job_id;
   ref-shaped for branch; word for verdict/phase/reviewer_model) — each value must
   additionally carry no VAULT-shape content (provider tokens, conn URIs, sshpass,
   env-assignment, cred-record token, denylist digest). classify() skips span/escalation
   pairs that fall inside exempt spans. Everything else still classified.
2. **Underscore-aware sha context:** `_SHA_CONTEXT_RE` extends `(?:\b|_)` on both ends
   so `head_sha`/`last_good_head_sha` windows stop triggering hex64 on their own.
3. **Numeric pass tally:** in the `prose_pass` branch, skip values matching
   `^(?:\d+[/|.])+\d+%?$|^\d+%$` (counts, ratios, percentages are not passwords).
4. **Macro placeholders:** `_PLACEHOLDER_RE` gains `__\w+__` (the `__NAME_API_KEY__`
   convention, alongside the existing `<...>` and `${VAR}` forms).

## Tests (red first) — `tests/test_status_ids_not_secrets_20261102.py`
- RED: the audit's full JSON body → must be CLEAN; `head_sha` alone; branch; tally;
  `KEY=__NAME_API_KEY__` (must not VAULT).
- Round-trip: `redact_content(body)` returns the body byte-identical once green.
- Both directions / real secrets still vaulted: body carrying `ghp_…` under a
  NON-allowlisted key → VAULT; real token placed as the value of `job_id` → the
  type/value guard refuses the exemption → still matches; `KEY=Tr0ub4dor&3xyz` →
  VAULT stays; known denylist placeholder literal keeps working.
- Guard tests: non-JSON prose unchanged; prefix-truncated "sha" keyword does not
  exempt an apples "payload hash" hex WITH keyword still exempted (existing behavior,
  pinned) and a value missing under a NON-status key (e.g. `data`) still REDACT.

## Risks
- permit-broadening: only keyword-adjacent (underscore) contexts and macro-shaped
  values change; denylist/prob-token families untouched.
- `verify_prefilter_superset()` and the existing golden tests must stay green.
- Plan-only gate content changes should not be auto_VAULTed meanwhile for shas;
  runbooks may include `head_sha` values — expected and intended.
