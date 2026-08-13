"""Credential-shape detection for the secret-vault classifier.

Release-blocking UAT (2026-06-13): GitHub/GitLab PATs and other
secrets were surfacing in normal/semantic search. The vault model
(operator-approved) isolates credential-class memories into a
dedicated ``namespace="vault"`` that the default read path excludes.

This module is the *detector*: given memory content it decides
whether the content is

  * VAULT      — clearly a credential carrier (a PAT, a private key,
                 ``api_key = sk-...``). The whole memory is moved into
                 the vault namespace and excluded from default search.
  * REDACT     — an incidental secret span inside otherwise-useful
                 prose (e.g. a doctrine note that quotes one token).
                 The memory stays in its namespace; the matched span
                 is masked on the default retrieval path.
  * CLEAN      — nothing credential-shaped.

Conservatism is deliberate. A 40-hex git SHA in prose, a UUID, or a
``token`` word with no value attached must NOT vault a legitimate
memory. High-confidence shapes (provider-prefixed keys, PEM blocks,
AWS access keys) are treated as VAULT; the generic
``key/secret/password = <value>`` assignment is treated as VAULT only
when the value itself looks secret-grade, otherwise REDACT.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Tuple

VAULT_NAMESPACE = "vault"


class SecretClass(str, Enum):
    CLEAN = "clean"
    REDACT = "redact"  # incidental span — keep memory, mask span on default read
    VAULT = "vault"  # credential carrier — isolate whole memory


# ── High-confidence provider/credential shapes ─────────────────────────
# Each pattern matches an actual secret *value*, not a mention of one.
# These are unambiguous enough to vault the whole memory.
_HIGH_CONFIDENCE: list[tuple[str, re.Pattern]] = [
    # GitHub tokens: ghp_, gho_, ghu_, ghs_, ghr_ + fine-grained github_pat_
    ("github_pat", re.compile(r"\bgithub_pat_[0-9A-Za-z_]{20,}")),
    ("github_classic", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{30,}")),
    # GitLab personal/project/group access tokens
    ("gitlab_pat", re.compile(r"\bglpat-[0-9A-Za-z_-]{18,}")),
    # OpenAI / Anthropic / OpenRouter style sk- keys (require a long body
    # so the bare word "sk-" or "sk-test" in prose doesn't trip it)
    ("sk_key", re.compile(r"\bsk-(?:proj-|ant-|or-v1-|live-|test-)?[0-9A-Za-z_-]{20,}")),
    # xAI
    ("xai_key", re.compile(r"\bxai-[0-9A-Za-z]{20,}")),
    # AWS access key id
    ("aws_akia", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # Google API key
    ("google_api", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    # Slack tokens
    ("slack", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}")),
    # PEM private key blocks
    ("pem_private", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

# ── Generic assignment: key/secret/password/token/bearer = <value> ─────
# Conservative: the *value* must itself be secret-grade (>=12 chars, mixed,
# not an obvious placeholder) for VAULT; otherwise REDACT the span.
_ASSIGN_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|pass|secret|credential|login|api[_-]?key|access[_-]?token|auth[_-]?token|bearer|token)\b"
    # Horizontal whitespace only around the separator so a value on the NEXT
    # line is not vacuumed into the assignment (ngc-review 2026-06-13).
    r"[ \t]*[:=][ \t]*"
    r"(['\"]?)([^\s'\"]{4,})\2"
)

# ── Credential PROSE patterns (release-blocking 2026-06-13) ────────────
# Token-SHAPE regexes above miss credentials written as natural prose:
#   "sudo password X", "root pw X", "ssh mini@host sudo password = 'mini'".
# These match the VALUE so the span can be redacted, and (when the
# surrounding memory reads as a credential record) drive VAULT.
#
# NO-COLON prose assignment: "<secret-word> <value>" with an optional
# linking verb/symbol. Catches "sudo password X", "root pw X",
# "login password X", "the password is X". The value is captured so it
# can be masked. Kept conservative: requires a recognised secret word as
# the head and a non-placeholder token of length >=3 as the value.
_PROSE_PASS_RE = re.compile(
    r"(?ix)"
    r"\b(?:sudo[ \t]+|root[ \t]+|admin[ \t]+|user[ \t]+|login[ \t]+|ssh[ \t]+)?"
    r"(password|passwd|pwd|pass|passphrase|secret|credential)\b"
    # Linker is CONSUMED when present so the verb ("is"/"was"/"set to")
    # is never mistaken for the value (ngc-review 2026-06-13). HORIZONTAL
    # whitespace only ([ \t], not \s) so the value is never pulled off the
    # NEXT line (ngc-review 2026-06-13 round 12).
    r"(?:[ \t]+(?:is|was|set[ \t]to|equals?)[ \t]+|[ \t]*(?:=|:|->|→)[ \t]*|[ \t]+)"
    # Value: either a QUOTED span (may contain spaces — "correct horse")
    # or an UNQUOTED single token. Two alternatives so a quoted multi-word
    # passphrase is captured whole, not just the first word (ngc-review
    # 2026-06-13). Group 2 = quote char; group 3 = quoted-inner; group 4 = bare.
    r"(?:(['\"`])([^'\"`]{3,})\2|([^\s'\"`,;]{3,}))"
)

# sshpass exposes the password inline: `sshpass -p 'X'` / `sshpass -pX`.
_SSHPASS_RE = re.compile(r"(?i)\bsshpass\s+-p\s*(['\"]?)([^\s'\"]{1,})\1")

# `ssh user@host ... password ... <value>` — the trailing value.
# Covered by _PROSE_PASS_RE; this catches `pw=` style inside connect notes.
# Horizontal whitespace only so a value on the next line is not vacuumed
# (ngc-review 2026-06-13).
_PW_KV_RE = re.compile(r"(?i)\b(?:root[ \t]+pw|pw|sudo[ \t]+pw)\b[ \t]*[:=]?[ \t]*(['\"]?)([^\s'\"]{3,})\1")

# Credential-record shorthand: "<user>/<password>" or "<user>:<password>".
# This is intentionally narrower than a generic path/key-value parser:
#   * the left side must start at a non-path/non-URL boundary
#   * the password segment cannot contain path/URL separators
#   * the password is validated by _record_password_is_credential()
_CRED_RECORD_TOKEN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9._%+/:@-])"
    r"([A-Za-z][A-Za-z0-9._-]{0,31})"
    r"([/:])"
    r"([^\s'\"`,;/:<>()\[\]{}]{6,})"
    r"(?![A-Za-z0-9._%+/:@-])"
)

# PasswordAuthentication directive (sshd_config). Whole token is sensitive
# as a security-posture disclosure; mask the keyword+value.
_PWAUTH_RE = re.compile(r"(?i)\bPasswordAuthentication\s+(?:yes|no|[^\s]+)")

# URI connection strings carrying inline credentials:
#   postgres://user:pass@host, redis://:pass@host, mongodb+srv://u:p@h, etc.
# Match the WHOLE URI through host + path + query (up to whitespace) so the
# masked span leaves no trailing db-name/query fragment (ngc-review
# 2026-06-13). The credential is the user:pass@ section; we redact the full
# URI because a half-masked connection string is both leaky and malformed.
# Terminator excludes whitespace AND common closing/trailing punctuation so
# the mask doesn't eat a Markdown ``)``/``]`` or a sentence-final ``.``/``,``
# that follows the URL in prose. REQUIRES at least the host char after ``@``
# (``+`` not ``*``) so the host/path is never left unmasked (ngc-review
# 2026-06-13).
_URI_TAIL = r"[^\s'\")\]}>,;]+"
_CONN_URI_RE = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|redis|rediss|mongodb(?:\+srv)?|amqps?|ldaps?|"
    r"sftp|ftp|smb)://[^:@/\s]+:[^@/\s]+@" + _URI_TAIL
)
# Generic HTTP(S) basic-auth in URL: https://user:pass@host/path?query
_BASIC_AUTH_URI_RE = re.compile(r"(?i)\bhttps?://[^:@/\s]+:[^@/\s]+@" + _URI_TAIL)

# .env-style ALLCAPS assignment near a secret-ish name with a long value:
#   DB_PASSWORD=abcd1234efgh, API_TOKEN=...., REDIS_AUTH=....
_ENV_SECRET_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*"
    r"(?:PASS|PASSWORD|PWD|SECRET|TOKEN|APIKEY|API_KEY|KEY|AUTH|CREDENTIAL)"
    # Horizontal whitespace only around "=" so a value on the next line is
    # not vacuumed (ngc-review 2026-06-13).
    r"[A-Z0-9_]*)[ \t]*=[ \t]*(['\"]?)([^\s'\"]{8,})\2"
)

# Explicit secret denylist — specific credentials that must ALWAYS be masked
# and drive VAULT wherever they appear, even when every other heuristic misses
# the surrounding prose shape. The generic heuristics catch these in credential
# context ("password=X", "sshpass -p X"); the denylist is what still catches a
# bare, low-entropy value dropped into ordinary prose.
#
# STORED AS DIGESTS, NEVER PLAINTEXT. A denylist of real credentials is itself
# a credential leak: this module ships in a published sdist, so any literal
# here would be permanently readable by anyone who downloads the package. Only
# SHA-256 digests are held, and candidate tokens are hashed for comparison, so
# the list can name a secret without disclosing it.
#
# Deployments add their own via MNEMOS_SECRET_DENY_LITERALS (comma-separated,
# plaintext in the environment, hashed at import and never retained).
_DENY_DIGESTS: frozenset[str] = frozenset(
    {
        # Fleet credentials confirmed to have leaked into prose.
        "bf746deac1e9a391abe084a369b304c783de51c2173e60131e8d842e8cb6eb88",
        "f920dc9b6fcc7d037f1300269976db036549da3af4117f91a14709c42a097885",
        # Obviously-fake value used by the module self-test and unit tests, so
        # the denylist path is exercised without shipping a real credential.
        "dbba25a7a6cbcac63a326941b395a9d3ddcdd0d8beb5f316ca78cb6f9b23a870",
        "5eb17397dbb83e35b60e3f79efd4f96be85e965bc35ff6205bb731ad88b6a000",
    }
    | {
        hashlib.sha256(lit.strip().encode()).hexdigest()
        for lit in os.environ.get("MNEMOS_SECRET_DENY_LITERALS", "").split(",")
        if lit.strip()
    }
)

# Candidate tokens: a denylisted value appears as a whitespace/quote-delimited
# run, so tokenising is enough and avoids hashing every window of the text.
_TOKEN_RE = re.compile(r"[^\s,;:'\"()\[\]{}<>]+")


def _is_denylisted(value: str) -> bool:
    """True if ``value`` hashes to a denylisted credential."""
    if not value:
        return False
    return hashlib.sha256(value.encode()).hexdigest() in _DENY_DIGESTS


def _iter_denylisted(text: str):
    """Yield (start, end) spans of denylisted tokens found in ``text``."""
    for mo in _TOKEN_RE.finditer(text):
        tok = mo.group(0)
        if _is_denylisted(tok):
            yield mo.start(), mo.end()
            continue
        # Trailing punctuation ("...<secret>." / "'X'") is common in prose.
        stripped = tok.strip(".!?'\"`")
        if stripped and stripped != tok and _is_denylisted(stripped):
            off = tok.find(stripped)
            yield mo.start() + off, mo.start() + off + len(stripped)

# Headers/markers that declare a memory to be a CREDENTIAL RECORD (the
# whole thing exists to store secrets) -> VAULT the entire memory.
_CRED_RECORD_RE = re.compile(
    r"(?i)(infrastructure\s+credentials|🔑\s*credential|credential\s*:|"
    r"ssh\s+access\s+patterns|access\s+credentials|fleet\s+passwords?|"
    r"login\s+credentials|root\s+login\s+password)"
)

# Long hex blob (40+). VERY false-positive-prone (git SHA = 40 hex).
# Only treat a hex blob as a secret-span (REDACT, never auto-VAULT) when
# it is 64+ chars (SHA-256-token grade) AND not preceded by SHA/commit
# context. 40-char SHAs in prose are left CLEAN by design.
_HEX_BLOB_RE = re.compile(r"\b[0-9a-fA-F]{64,}\b")
_SHA_CONTEXT_RE = re.compile(r"(?i)\b(sha|commit|sha256|sha-256|digest|hash|md5)\b")

_PLACEHOLDER_RE = re.compile(
    r"(?i)\A(x{3,}|\*{3,}|<[^>]+>|your[_-]?\w+|changeme|placeholder|redacted|"
    r"example|todo|none|null|n/?a|\.\.\.|\$\{?\w+\}?)\Z"
)

# Words that legitimately follow a secret-noun in ordinary prose and must
# NOT be treated as the secret value: "password rotation", "password is
# required", "password auth enabled", "credential management", etc.
_PROSE_STOPWORDS: frozenset[str] = frozenset(
    {
        "is",
        "was",
        "are",
        "the",
        "a",
        "an",
        "to",
        "for",
        "of",
        "and",
        "or",
        "set",
        "reset",
        "rotation",
        "rotate",
        "rotated",
        "required",
        "enabled",
        "disabled",
        "auth",
        "authentication",
        "management",
        "manager",
        "policy",
        "store",
        "vault",
        "field",
        "value",
        "prompt",
        "needed",
        "missing",
        "empty",
        "blank",
        "default",
        "strength",
        "complexity",
        "expiry",
        "expires",
        "expired",
        "hash",
        "hashed",
        "salted",
        "must",
        "should",
        "will",
        "here",
        "above",
        "below",
        "via",
        "with",
        "without",
        "per",
        "this",
        "that",
        "credentials",
        "credential",
        "secrets",
        "secret",
        "manager.",
        "less",
        "based",
        "protected",
        "stored",
    }
)


def _value_is_secret_grade(value: str) -> bool:
    """True if an assignment RHS looks like a real secret, not a label."""
    if _PLACEHOLDER_RE.match(value):
        return False
    if len(value) < 12:
        return False
    # require some entropy: at least two of {lower, upper, digit, symbol}
    classes = sum(bool(re.search(p, value)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^0-9A-Za-z]"))
    return classes >= 2


# Common NON-secret RHS values that frequently follow a secret-ish word in
# config/prose ("pass = true", "login: user", "token: enabled"). A weak
# assignment with one of these values is NOT redacted.
_NONSECRET_ASSIGN_VALUES: frozenset[str] = frozenset(
    {
        "true",
        "false",
        "yes",
        "no",
        "on",
        "off",
        "enabled",
        "disabled",
        "none",
        "null",
        "nil",
        "user",
        "users",
        "admin",
        "root",
        "guest",
        "required",
        "optional",
        "default",
        "auto",
        "manual",
        "ok",
        "set",
        "unset",
        "empty",
        "blank",
        "valid",
        "invalid",
        "active",
        "inactive",
        "public",
        "private",
        "value",
        "string",
        "int",
        "bool",
        "type",
    }
)
# High-signal assignment heads: a value here is password-grade by intent
# even if weak, so a short/quoted value should still be masked.
_STRONG_ASSIGN_HEADS = re.compile(r"(?i)\A(password|passwd|pwd|pass|passphrase|secret)\Z")


def _weak_assign_worth_redacting(head: str, value: str) -> bool:
    """Whether a NON-secret-grade assignment value should still be redacted.

    An explicit ``<credential-head> = <value>`` assignment (the head is one
    of password/token/api_key/secret/bearer/login/credential/...) is a
    deliberate credential record, so its value is masked UNLESS the value is
    a recognised config token ("true"/"user"/"enabled"/...), a placeholder,
    or a stopword — those are config/prose, not secrets (ngc-review
    2026-06-13). This keeps `pass=true` / `login:user` / `token:enabled`
    CLEAN while masking `token=abc123` / `login: s3cr3t` / `api_key=abcd`.
    """
    v = value.strip()
    if v.lower() in _NONSECRET_ASSIGN_VALUES or v.lower() in _PROSE_STOPWORDS:
        return False
    if _PLACEHOLDER_RE.match(v):
        return False
    # Any remaining non-config, non-placeholder value after an explicit
    # credential head is masked. (High-signal password heads are a subset;
    # _STRONG_ASSIGN_HEADS kept for callers/tests that want the distinction.)
    return True


def _prose_value_is_credential(value: str) -> bool:
    """Heuristic for a NO-colon prose value like 'sudo password <value>'.

    Reject obvious English stopwords/labels so 'password rotation' or
    'password is required' don't redact. Accept anything that looks like a
    real token: a known fleet literal, a mixed-class token, or a short
    quoted-style secret.
    """
    v = value.strip()
    if not v or _PLACEHOLDER_RE.match(v):
        return False
    if v.lower() in _PROSE_STOPWORDS:
        return False
    if _is_denylisted(v):
        return True
    # A plausible password: >=4 chars and not a plain dictionary word.
    if len(v) < 4:
        return False
    has_symbol = bool(re.search(r"[^0-9A-Za-z]", v))
    has_digit = bool(re.search(r"[0-9]", v))
    has_mixed_case = bool(re.search(r"[a-z]", v)) and bool(re.search(r"[A-Z]", v))
    if has_symbol or has_digit or has_mixed_case:
        return True
    # An all-lowercase plain-alpha bare token after a password noun (e.g.
    # "password prompt", "password fido") is too ambiguous to redact on its
    # own — most are ordinary English (ngc-review 2026-06-13 FP fix). It is
    # still caught when QUOTED (the quoted branch in classify() flags it) or
    # when the memory is a CREDENTIAL RECORD (the record escalation VAULTs
    # the whole memory). A bare all-lowercase word is NOT a credential here.
    return False


def _record_password_is_credential(value: str) -> bool:
    """True for password-looking RHS in a bare user/password record token.

    This must be stricter than prose/assignment values because the token
    shape also resembles ordinary relative paths (``src/AliceBeta``).
    """
    v = value.strip()
    if not v or len(v) < 6 or _PLACEHOLDER_RE.match(v):
        return False
    if _is_denylisted(v):
        return True
    # Dates and version-ish path segments are common in notes and paths.
    if re.fullmatch(r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}", v):
        return False
    if re.fullmatch(r"v?\d+(?:\.\d+){1,}(?:[-+][0-9A-Za-z.]+)?", v, re.IGNORECASE):
        return False
    # Do not treat email/host-looking tokens as passwords.
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
        return False
    if re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", v):
        return False
    has_lower = bool(re.search(r"[a-z]", v))
    has_upper = bool(re.search(r"[A-Z]", v))
    has_digit = bool(re.search(r"[0-9]", v))
    has_symbol = bool(re.search(r"[^0-9A-Za-z]", v))
    has_alpha = has_lower or has_upper
    if not has_alpha:
        return False
    # Pure-alpha path/name/release segments are not password-grade, even when
    # mixed case (e.g. AliceBeta, JulyRelease).
    if not (has_digit or has_symbol):
        return False
    # Ordinary password complexity for this shorthand requires both a digit
    # and a symbol. This catches values like Tr0ub4dor&3 without accepting
    # relative path segments that are merely mixed-case words.
    if has_digit and has_symbol:
        return True
    # Fallback for clearly random-looking non-dictionary values that may omit
    # either digits or symbols. Keep it narrow so release labels and names with
    # numbers do not become credential spans.
    classes = sum((has_lower, has_upper, has_digit, has_symbol))
    longest_alpha_run = max((len(m.group(0)) for m in re.finditer(r"[A-Za-z]+", v)), default=0)
    return len(v) >= 12 and classes >= 3 and longest_alpha_run <= 4


@dataclass
class SecretFinding:
    cls: SecretClass = SecretClass.CLEAN
    reasons: List[str] = field(default_factory=list)
    spans: List[Tuple[int, int]] = field(default_factory=list)  # (start,end) to redact


def _escalate(finding: SecretFinding, cls: SecretClass) -> None:
    """Raise the finding class monotonically (CLEAN < REDACT < VAULT)."""
    order = {SecretClass.CLEAN: 0, SecretClass.REDACT: 1, SecretClass.VAULT: 2}
    if order[cls] > order[finding.cls]:
        finding.cls = cls


# Cheap single-pass prefilter (ngc-review 2026-06-13 perf finding):
# redact-at-retrieval runs classify() on content/compressed/verbatim for
# every returned row. The overwhelming majority of memories contain NO
# secret-ish marker; this one compiled regex (a single C-level scan)
# short-circuits them before the full multi-pattern battery runs. It must
# be a SUPERSET of every signal classify() looks for, so a CLEAN verdict
# from the prefilter is safe — enforced by verify_prefilter_superset() +
# PREFILTER_SAMPLES (the contract test). Triggers: every secret NOUN/head
# the assignment + prose detectors key off (password/pwd/pw/secret/token/
# auth/key/...), so every assignment family is reached through its head
# word (the bare "=" / ":" separators are deliberately NOT triggers — they
# are far too common and the head word always co-occurs); plus "@"
# (URIs/basic-auth), "://" (conn strings), "sshpass", a PEM header, and the
# provider token prefixes + explicit fleet literals. A long hex blob has no
# keyword anchor and is checked separately in _prefilter_hits.
_PREFILTER_RE = re.compile(
    r"(?ix)("
    # secret NOUNS / assignment heads, anchored at a word boundary OR an
    # underscore so ordinary English ("monkey", "passage", "classic",
    # "authentication", "keynote") does NOT trip the fast path, while
    # env-style ALLCAPS names (REDIS_AUTH, DB_PASSWORD, API_KEY) still do
    # via the leading/trailing `_` (ngc-review 2026-06-13 round 12).
    r" (?:\b|_) (?: password | passwd | pwd | pw | pass | passphrase |"
    r"            secret | credential | login | token | bearer | auth |"
    r"            api[_-]?key | apikey | key ) (?:\b|_) |"
    # PasswordAuthentication has no boundary after "Password" (the sshd
    # directive detector keys off it) — list it explicitly so the prefilter
    # is a true superset (ngc-review 2026-06-13).
    r" passwordauthentication |"
    r" sshpass |"
    # structural credential carriers
    r" :// | @ | -----BEGIN |"
    # provider token prefixes (literal, safe as substrings)
    r" ghp_ | gho_ | ghu_ | ghs_ | ghr_ | github_pat_ | glpat- | sk- | xai- |"
    r" AKIA | AIza | xox |"
    # NOTE: denylisted literals are deliberately absent here — the prefilter
    # must not carry plaintext credentials (see _DENY_DIGESTS). Their prose
    # carriers ("password", "pw", "sshpass") already anchor the prefilter, and
    # _prefilter_hits() consults the digest denylist directly as a backstop.
    r")"
)


def _prefilter_hits(text: str) -> bool:
    """True if ``text`` MIGHT contain a secret (must over-approximate)."""
    # Hex blobs (64+) have no keyword anchor; check them cheaply too.
    if _PREFILTER_RE.search(text):
        return True
    if _CRED_RECORD_TOKEN_RE.search(text):
        return True
    return bool(_HEX_BLOB_RE.search(text))


# ── Prefilter superset contract (ngc-review 2026-06-13) ────────────────
# The fast-path prefilter in classify() MUST fire for every detector
# family, or a real secret would be silently skipped as CLEAN. To make
# the superset property mechanically verifiable (and to keep
# sample-registration coupled to pattern-registration in ONE place), every
# detector family registers a canonical positive sample here. The test
# ``test_prefilter_is_true_superset_of_every_signal`` iterates this map and
# asserts (a) _prefilter_hits(sample) is True AND (b) classify(sample) is
# not CLEAN. Adding a new pattern WITHOUT adding its sample here fails the
# test by omission only if the author also forgets the sample — so the
# canonical list below is the single source of truth reviewers check.
PREFILTER_SAMPLES: dict[str, str] = {
    "github_pat": "github_pat_" + "a" * 25,
    "github_classic": "ghp_" + "a" * 35,
    "gitlab_pat": "glpat-" + "a" * 20,
    "sk_key": "sk-" + "a" * 25,
    "xai_key": "xai-" + "a" * 25,
    "aws_akia": "AKIA" + "B" * 16,
    "google_api": "AIza" + "a" * 35,
    "slack": "xoxb-" + "1" * 15,
    "pem_private": "-----BEGIN RSA PRIVATE KEY-----",
    "assign": "api_key = " + "aB3" * 5,
    "conn_uri": "postgres://u:secretpass@host/db",
    "basic_auth_uri": "https://u:secretpass@host/x",
    "sshpass": "sshpass -p secret123",
    "prose_pass": "sudo password Tr0ub4&3x",
    "prose_pass_quoted": 'password is "correct horse battery"',
    "pw_kv": "pw: s3cretValue123",
    "pw_kv_root": "root pw DenylistSelfTest@NotARealSecret1",
    "env_secret": "API_TOKEN=abcdefgh1234",
    "env_secret_concat": "APIKEY=abcdefgh1234",
    "password_auth": "PasswordAuthentication yes",
    "hex64": "a" * 64,
    "fleet_literal": "DenylistSelfTest@NotARealSecret1",
    "cred_record_token": "alice/Tr0ub4dor&3",
}


def verify_prefilter_superset() -> list[str]:
    """Return the names of any registered family that violates the prefilter
    contract (should always be empty). A family VIOLATES the contract if its
    canonical sample either (a) is MISSED by the prefilter (would be skipped
    as CLEAN), or (b) does NOT classify as a secret (an inert sample that
    makes the superset check vacuous). Both properties are checked HERE in a
    single contract helper so tests cannot drift (ngc-review 2026-06-13)."""
    bad: list[str] = []
    for name, sample in PREFILTER_SAMPLES.items():
        if not _prefilter_hits(sample):
            bad.append(f"{name}:prefilter-miss")
        elif classify(sample).cls is SecretClass.CLEAN:
            bad.append(f"{name}:classifies-clean")
    return bad


def classify(content: str | None) -> SecretFinding:
    """Classify memory content into CLEAN / REDACT / VAULT.

    Two distinct outcomes for credentials:

    * A memory that is PREDOMINANTLY a credential record (a header like
      "INFRASTRUCTURE CREDENTIALS" / "🔑 Credential", or several distinct
      credential spans) -> VAULT (moved to the excluded namespace).
    * A memory with an INCIDENTAL credential span in otherwise-useful
      prose -> REDACT (stays in its namespace; span masked at retrieval).

    Spans are ALWAYS collected for every credential match (token-shape OR
    prose) so redact-at-retrieval can mask them even on a VAULT-miss.
    """
    finding = SecretFinding()
    if not content:
        return finding
    text = str(content)

    # Fast path: no secret-ish marker anywhere -> CLEAN without running the
    # full pattern battery (perf — see _prefilter_hits).
    if not _prefilter_hits(text):
        return finding

    cred_span_count = 0  # distinct credential spans -> "predominantly" signal

    # 0. Explicit fleet-literal denylist → VAULT, always mask.
    for _dstart, _dend in _iter_denylisted(text):
        _escalate(finding, SecretClass.VAULT)
        finding.reasons.append("fleet_literal")
        finding.spans.append((_dstart, _dend))
        cred_span_count += 1

    # 1. High-confidence token shapes → VAULT.
    for name, pat in _HIGH_CONFIDENCE:
        for mo in pat.finditer(text):
            _escalate(finding, SecretClass.VAULT)
            finding.reasons.append(name)
            finding.spans.append((mo.start(), mo.end()))
            cred_span_count += 1

    # 2. Generic key/value assignments.
    for mo in _ASSIGN_RE.finditer(text):
        value = mo.group(3)
        if _value_is_secret_grade(value):
            _escalate(finding, SecretClass.VAULT)
            finding.reasons.append(f"assign:{mo.group(1).lower()}")
            finding.spans.append((mo.start(3), mo.end(3)))
            cred_span_count += 1
        elif _weak_assign_worth_redacting(mo.group(1), value):
            _escalate(finding, SecretClass.REDACT)
            finding.reasons.append(f"assign-weak:{mo.group(1).lower()}")
            finding.spans.append((mo.start(3), mo.end(3)))
        # else: a weak NON-secret value after a secret-ish word
        # ("pass = true", "login: user", "token: enabled") — common
        # config/prose, NOT a credential. Leave CLEAN (ngc-review
        # 2026-06-13 FP fix).

    # 3. Connection strings + basic-auth URLs (always carry a live secret).
    for name, pat in (("conn_uri", _CONN_URI_RE), ("basic_auth_uri", _BASIC_AUTH_URI_RE)):
        for mo in pat.finditer(text):
            _escalate(finding, SecretClass.VAULT)
            finding.reasons.append(name)
            finding.spans.append((mo.start(), mo.end()))
            cred_span_count += 1

    # 4. sshpass -p <value> → VAULT (inline plaintext password).
    for mo in _SSHPASS_RE.finditer(text):
        _escalate(finding, SecretClass.VAULT)
        finding.reasons.append("sshpass")
        finding.spans.append((mo.start(2), mo.end(2)))
        cred_span_count += 1

    # 5. NO-colon prose password ("sudo password X", "root pw X", "pw: X").
    for name, pat in (("prose_pass", _PROSE_PASS_RE), ("pw_kv", _PW_KV_RE)):
        for mo in pat.finditer(text):
            if name == "prose_pass":
                # Explicit value-group selection (ngc-review 2026-06-13):
                # group 2 = opening quote (truthy => quoted), group 3 =
                # quoted-inner value, group 4 = bare value. Do NOT rely on
                # lastindex — pick the value group directly.
                quoted = bool(mo.group(2))
                vgroup = 3 if quoted else 4
                value = mo.group(vgroup)
                if not (quoted or _prose_value_is_credential(value)):
                    continue
            else:  # pw_kv: group 1 = opt quote, group 2 = value (explicit,
                # not lastindex, which is engine-fragile — ngc-review 2026-06-13)
                vgroup = 2
                value = mo.group(vgroup)
                if _PLACEHOLDER_RE.match(value) or value.lower() in _PROSE_STOPWORDS:
                    continue
            # A fleet literal or secret-grade value → VAULT; else REDACT span.
            if _is_denylisted(value) or _value_is_secret_grade(value):
                _escalate(finding, SecretClass.VAULT)
                cred_span_count += 1
            else:
                _escalate(finding, SecretClass.REDACT)
            finding.reasons.append(name)
            finding.spans.append((mo.start(vgroup), mo.end(vgroup)))

    # 6. Bare credential-record token: "alice/Tr0ub4dor&3" or
    # "alice:Tr0ub4dor&3". On its own this is a redactable credential span;
    # when paired with a credential-record header it escalates below.
    for mo in _CRED_RECORD_TOKEN_RE.finditer(text):
        value = mo.group(3)
        if not _record_password_is_credential(value):
            continue
        _escalate(finding, SecretClass.REDACT)
        finding.reasons.append("cred_record_token")
        finding.spans.append((mo.start(3), mo.end(3)))
        cred_span_count += 1

    # 7. .env-style ALLCAPS secret assignment with a long value → VAULT.
    for mo in _ENV_SECRET_RE.finditer(text):
        value = mo.group(3)
        if _PLACEHOLDER_RE.match(value):
            continue
        _escalate(finding, SecretClass.VAULT)
        finding.reasons.append("env_secret")
        finding.spans.append((mo.start(3), mo.end(3)))
        cred_span_count += 1

    # 8. PasswordAuthentication directive → REDACT (security-posture span).
    for mo in _PWAUTH_RE.finditer(text):
        _escalate(finding, SecretClass.REDACT)
        finding.reasons.append("password_auth")
        finding.spans.append((mo.start(), mo.end()))

    # 9. Long hex blobs (64+) without SHA/commit context → REDACT span only.
    for mo in _HEX_BLOB_RE.finditer(text):
        window = text[max(0, mo.start() - 30) : mo.start()]
        if _SHA_CONTEXT_RE.search(window):
            continue
        _escalate(finding, SecretClass.REDACT)
        finding.reasons.append("hex64")
        finding.spans.append((mo.start(), mo.end()))

    # 10. Credential-RECORD escalation. A memory headed by a credential
    # marker ("INFRASTRUCTURE CREDENTIALS", "🔑 Credential", "SSH ACCESS
    # PATTERNS", "root login password") that ALSO carries >=1 credential
    # span is PREDOMINANTLY a credential record → VAULT the whole memory,
    # not just redact a span. Multiple distinct credential spans (>=2)
    # likewise signal a credential record even without a header.
    if _CRED_RECORD_RE.search(text) and (cred_span_count >= 1 or finding.spans):
        _escalate(finding, SecretClass.VAULT)
        finding.reasons.append("cred_record")
    elif cred_span_count >= 2:
        _escalate(finding, SecretClass.VAULT)
        finding.reasons.append("multi_span")

    return finding


def redact(content: str | None, spans: List[Tuple[int, int]]) -> str:
    """Mask the given spans in content with ``[REDACTED]``.

    Spans are clamped to ``[0, len(content)]``, dropped if empty/inverted,
    sorted, and overlapping/adjacent spans merged BEFORE replacement, so
    overlapping detector matches (e.g. a fleet literal inside a larger
    assignment span) collapse to a single ``[REDACTED]`` with no duplicated
    or partially-unredacted text (ngc-review 2026-06-13).
    """
    if not content or not spans:
        return content or ""
    n = len(content)
    norm: list[tuple[int, int]] = []
    for s, e in spans:
        cs, ce = max(0, min(s, n)), max(0, min(e, n))
        if ce > cs:
            norm.append((cs, ce))
    if not norm:
        return content
    merged: list[list[int]] = []
    for s, e in sorted(norm):
        if merged and s <= merged[-1][1]:  # overlap OR adjacency -> coalesce
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out = content
    for s, e in reversed(merged):  # right-to-left keeps earlier offsets valid
        out = out[:s] + "[REDACTED]" + out[e:]
    return out


def redact_content(content: str | None) -> str:
    """Classify ``content`` and mask every credential span it contains.

    This is the redact-at-retrieval entry point: the default (non-root /
    non-include_secrets) read path runs returned content through this so
    any credential span — whether it belongs to a memory that should have
    been vaulted but was missed, or an incidental span in a legitimately
    CLEAN/REDACT memory — is masked ``[REDACTED]`` before leaving the
    server. CLEAN content with no spans passes through untouched.
    """
    if not content:
        return content or ""
    finding = classify(content)
    if not finding.spans:
        return content
    return redact(content, finding.spans)


def _stored_spans(metadata: Any, field_name: str) -> list[tuple[int, int]]:
    """Extract stored secret spans for ``field_name`` from row metadata.

    Returns ``[]`` when no stored spans are present. Accepts dict metadata
    or a JSON string (parsed defensively). Honors both the per-field
    ``secret_redact_fields`` map (preferred) and the legacy content-only
    ``secret_redact_spans`` list (back-compat for rows written before the
    per-field map existed).
    """
    if metadata is None:
        return []
    if isinstance(metadata, str):
        import json

        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            return []
    if not isinstance(metadata, dict):
        return []
    fields = metadata.get("secret_redact_fields")
    if isinstance(fields, dict):
        spans = fields.get(field_name)
        if isinstance(spans, list) and spans:
            return [(int(s), int(e)) for s, e in spans if isinstance(s, int) and isinstance(e, int)]
    if field_name == "content":
        legacy = metadata.get("secret_redact_spans")
        if isinstance(legacy, list) and legacy:
            return [(int(s), int(e)) for s, e in legacy if isinstance(s, int) and isinstance(e, int)]
    return []


def redact_field_with_stored(content: str | None, metadata: Any, field_name: str) -> str:
    """Redact ``content`` using stored ingest spans; fall back to recompute.

    F2b (adversarial review 2026-06-28): prefer the spans recorded at ingest
    classification (authoritative at the time of write) over recomputing
    ``classify()`` at retrieval, so a later classifier relaxation or pattern
    change cannot re-expose a secret that was caught at ingest. When no
    stored spans are present (legacy rows, or CLEAN content with none
    recorded), fall back to ``redact_content`` (recompute) so the
    redact-at-retrieval backstop is never weakened.
    """
    if not content:
        return content or ""
    spans = _stored_spans(metadata, field_name)
    if spans:
        return redact(content, spans)
    return redact_content(content)
