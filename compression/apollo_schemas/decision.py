"""Decision schema — "decided X because Y, alternatives considered Z".

Dense form:
    DECISION:chose=<choice>[|because=<rationale>][|over=<alt1,alt2,...>]

Example:
    DECISION:chose=postgres|because=transactional audit chain|over=sqlite,mongo

Detection is strict by design — schema fires only on content that
explicitly signals a decision (marker word required) AND produces a
clean capture of the choice. Ambiguous content passes through to
APOLLO's LLM fallback instead of landing a garbled dense form.

Rationale: false positives in dense form are worse than false
negatives. A bad schema match produces a dense encoding that misses
the meaning; a fallback to LLM extraction captures it properly.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from .base import DetectionResult, Schema


# Marker words that must appear before this schema even tries to match.
_DECISION_MARKERS = (
    "decided",
    "chose",
    "picked",
    "selected",
    "went with",
    "settled on",
    "opted for",
    "elected to",
    "rationale",
    "alternatives considered",
)

# Capture the choice: "<marker> (to|to use) <choice>" up to a stop word.
# Stop tokens fall into two groups:
#   - Content markers (need a whitespace prefix, like "\s+because")
#   - Punctuation + end-of-string (no prefix; "postgres." terminates on .)
_CHOSE_RE = re.compile(
    r"\b(?:decided|chose|picked|selected|went\s+with|settled\s+on|"
    r"opted\s+for|elected\s+to)\b"
    r"(?:\s+to(?:\s+use)?)?\s+"
    r"(?P<chose>[A-Za-z][\w\s\-./|]*?)"
    r"(?=\s+(?:because|since|due\s+to|given|over|instead|rather|"
    r"for\s+the|as\s+(?:it|they|the))|\s*[.,;!?]|\s*$)",
    re.IGNORECASE,
)

# Capture the rationale: "<marker> <rationale>" up to a stop word.
_BECAUSE_RE = re.compile(
    r"\b(?:because|since|due\s+to|given|rationale\s*:)\b\s*"
    r"(?P<because>[A-Za-z][^.;]*?)"
    r"(?=\s*(?:\.|;|over|alternatives|\s+instead|$))",
    re.IGNORECASE,
)

# Capture alternatives: "over X, Y" or "alternatives (considered/were): X, Y".
_ALTERNATIVES_RE = re.compile(
    r"\b(?:over|alternatives(?:\s+considered|\s+were)?)\b\s*:?\s*"
    r"(?P<alts>[A-Za-z0-9][^.;]*)",
    re.IGNORECASE,
)


class DecisionSchema(Schema):
    id = "decision"
    version = "0.1"

    def detect(self, content: str) -> Optional[DetectionResult]:
        if not content:
            return None

        lowered = content.lower()
        marker_hits = [m for m in _DECISION_MARKERS if m in lowered]
        if not marker_hits:
            return None

        chose_match = _CHOSE_RE.search(content)
        if chose_match is None:
            return None
        chose = _clean_capture(chose_match.group("chose"))
        if not chose:
            return None

        because_match = _BECAUSE_RE.search(content)
        because = (
            _clean_capture(because_match.group("because"))
            if because_match else None
        )

        alternatives: List[str] = []
        alts_match = _ALTERNATIVES_RE.search(content)
        if alts_match:
            alts_raw = alts_match.group("alts")
            # Split on comma or " and " (English lists); drop empties.
            alternatives = [
                _clean_capture(a)
                for a in re.split(r",|\s+and\s+", alts_raw)
                if a.strip()
            ]
            # Cap at 5 to keep the dense form compact.
            alternatives = [a for a in alternatives if a][:5]

        # Confidence scales with marker count + presence of rationale +
        # presence of alternatives. Baseline 0.6 on just the choice; up
        # to 1.0 with both rationale and 3+ alternatives.
        confidence = 0.6
        confidence += 0.03 * min(len(marker_hits), 3)
        if because:
            confidence += 0.15
        if alternatives:
            confidence += 0.05 * min(len(alternatives), 3)
        confidence = min(1.0, confidence)

        return DetectionResult(
            schema_id=self.id,
            schema_version=self.version,
            fields={
                "chose": chose,
                "because": because,
                "alternatives": alternatives,
            },
            confidence=confidence,
            original_length=len(content),
            notes=(
                f"chose={chose!r}, "
                f"because={bool(because)}, "
                f"{len(alternatives)} alternative(s)"
            ),
        )

    def encode(self, match: DetectionResult) -> str:
        parts: List[str] = [f"chose={_sanitize(match.fields['chose'])}"]
        if match.fields.get("because"):
            parts.append(f"because={_sanitize(match.fields['because'])}")
        alts = match.fields.get("alternatives") or []
        if alts:
            parts.append("over=" + ",".join(_sanitize(a) for a in alts))
        return "DECISION:" + "|".join(parts)

    def narrate(self, encoded: str) -> str:
        """Rule-based readback. S-III swaps in a small-LLM call."""
        if not encoded.startswith("DECISION:"):
            return encoded
        fields = _parse_pipe_fields(encoded[len("DECISION:"):])
        chose = fields.get("chose", "")
        because = fields.get("because")
        alts = fields.get("over")

        sentences: List[str] = []
        if chose:
            sentence = f"Chose {chose}"
            if because:
                sentence += f" because {because}"
            sentence += "."
            sentences.append(sentence)
        if alts:
            # Normalize comma-separated list to a readable form.
            parts = [a.strip() for a in alts.split(",") if a.strip()]
            if parts:
                sentences.append(f"Alternatives considered: {', '.join(parts)}.")
        return " ".join(sentences) if sentences else encoded


def _clean_capture(s: str) -> str:
    """Strip surrounding whitespace/punct from a capture group."""
    return s.strip().strip(".,;:").strip()


def _sanitize(s: str) -> str:
    """Strip characters that would break the pipe-delimited dense form."""
    return s.replace("|", "/").replace("\n", " ").strip()


def _parse_pipe_fields(payload: str) -> Dict[str, str]:
    """Parse ``key=v|key=v`` payload into a dict. Preserves the first
    occurrence of each key."""
    fields: Dict[str, str] = {}
    for seg in payload.split("|"):
        if "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        fields.setdefault(k.strip(), v.strip())
    return fields
