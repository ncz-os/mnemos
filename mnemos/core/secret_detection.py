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

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Tuple

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
    r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|bearer|token)\b"
    r"\s*[:=]\s*"
    r"(['\"]?)([^\s'\"]{6,})\2"
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


def _value_is_secret_grade(value: str) -> bool:
    """True if an assignment RHS looks like a real secret, not a label."""
    if _PLACEHOLDER_RE.match(value):
        return False
    if len(value) < 12:
        return False
    # require some entropy: at least two of {lower, upper, digit, symbol}
    classes = sum(bool(re.search(p, value)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^0-9A-Za-z]"))
    return classes >= 2


@dataclass
class SecretFinding:
    cls: SecretClass = SecretClass.CLEAN
    reasons: List[str] = field(default_factory=list)
    spans: List[Tuple[int, int]] = field(default_factory=list)  # (start,end) to redact


def classify(content: str | None) -> SecretFinding:
    """Classify memory content into CLEAN / REDACT / VAULT."""
    finding = SecretFinding()
    if not content:
        return finding
    text = str(content)

    # 1. High-confidence shapes → VAULT.
    for name, pat in _HIGH_CONFIDENCE:
        for mo in pat.finditer(text):
            finding.cls = SecretClass.VAULT
            finding.reasons.append(name)
            finding.spans.append((mo.start(), mo.end()))

    # 2. Generic assignments.
    for mo in _ASSIGN_RE.finditer(text):
        value = mo.group(3)
        if _value_is_secret_grade(value):
            # secret-grade value → vault the whole memory
            finding.cls = SecretClass.VAULT
            finding.reasons.append(f"assign:{mo.group(1).lower()}")
            finding.spans.append((mo.start(3), mo.end(3)))
        else:
            # weak value → at most redact the span, never vault
            if finding.cls is SecretClass.CLEAN:
                finding.cls = SecretClass.REDACT
            finding.reasons.append(f"assign-weak:{mo.group(1).lower()}")
            finding.spans.append((mo.start(3), mo.end(3)))

    # 3. Long hex blobs (64+) without SHA/commit context → REDACT span only.
    for mo in _HEX_BLOB_RE.finditer(text):
        window = text[max(0, mo.start() - 30) : mo.start()]
        if _SHA_CONTEXT_RE.search(window):
            continue  # explicitly a SHA/commit/digest — leave clean
        if finding.cls is SecretClass.CLEAN:
            finding.cls = SecretClass.REDACT
        finding.reasons.append("hex64")
        finding.spans.append((mo.start(), mo.end()))

    return finding


def redact(content: str | None, spans: List[Tuple[int, int]]) -> str:
    """Mask the given spans in content with ``[REDACTED]``."""
    if not content or not spans:
        return content or ""
    # merge + sort spans, replace from the right so offsets stay valid
    merged: list[list[int]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out = content
    for s, e in reversed(merged):
        out = out[:s] + "[REDACTED]" + out[e:]
    return out
