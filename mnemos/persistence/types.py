"""Shared persistence typing primitives."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, TypeAlias

JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
JSONObject: TypeAlias = dict[str, JSONValue]
Row: TypeAlias = Any


MEMORY_COLS = (
    "id, content, category, subcategory, created, updated, "
    "metadata, quality_rating, compressed_content, verbatim_content, "
    "owner_id, group_id, namespace, permission_mode, "
    "source_model, source_provider, source_session, source_agent, "
    "archived_at, consolidated_into"
)

# Backward-compatible persistence-layer alias for modules that still use the
# historical private constant spelling.
_MEMORY_COLS = MEMORY_COLS


def _coerce_text(value: Any) -> str | None:
    """Materialise a CLOB / LOB value to a plain ``str``.

    Async drivers (oracledb async, asyncpg with text-mode CLOB columns)
    sometimes return a coroutine-bearing LOB handle whose ``.read()``
    yields the bytes/text. Driver adapters are responsible for eagerly
    awaiting the read in their concrete repositories before the row
    reaches this helper; here we just unwrap the remaining str/bytes
    variants and treat everything else (including ``None``) as-is.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")
    return str(value)


def _coerce_json(value: Any) -> Any:
    """Return parsed JSON for a ``model_variants``-style column.

    Drivers hand this back as a dict/list (asyncpg JSONB, Oracle 23ai JSON
    type) or as a JSON string/CLOB (Oracle ``CLOB CHECK (... IS JSON)``,
    sqlite TEXT). Pass structured values through untouched; parse str/bytes;
    on invalid JSON keep the raw materialised text rather than raising.
    """
    if value is None or isinstance(value, (dict, list)):
        return value
    text = _coerce_text(value)
    if text is None:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def assemble_consultation_full(
    consultation: Row,
    audit_rows: list[Row],
) -> dict[str, Any]:
    """Assemble a classified verbatim view of one GRAEAE consultation.

    Both halves come from the concrete repository's two read queries:

    * ``consultation`` — the single ``graeae_consultations`` row for the
      id (already resolved by the caller; ``None`` is *not* handled
      here — see ``ConsultationsRepository.fetch_consultation_full``).
    * ``audit_rows`` — every ``graeae_audit_log`` row for that
      consultation, **ordered by ``sequence_num`` ascending** so the
      assembled ``muses`` list is in invocation order.

    Output shape (all CLOB / large-text columns materialised to ``str``):

        {
          "consultation_id": str,
          "source":    {prompt, context, task_type, mode, created},
          "quorum":    {consensus_score, winning_muse, cost, latency_ms,
                        model_variants, muses:[{provider, model,
                        quality_score, latency_ms}]},
          "synthesis": {text},
          "muses":     [{provider, model, response_text}],
        }

    Notes on truncation discipline: ``graeae_consultations.consensus_response``
    is truncated to 500 chars at WRITE time (see the ``create_consultation_with_audit``
    paths in each backend). When this happens the verbatim synthesis lives
    only on the winning-muse ``graeae_audit_log`` row; if
    ``consensus_response`` ends in an ellipsis or shorter than the
    corresponding audit-row response, callers should fall back to the
    winning-muse response_text. This helper surfaces both — the synthesis
    text from ``consensus_response`` and the full per-muse
    ``response_text`` — and never truncates either one.
    """
    consultation_id = consultation.get("id")
    prompt = _coerce_text(consultation.get("prompt")) or ""
    context = _coerce_text(consultation.get("context_uncompressed"))
    if context is None:
        context = _coerce_text(consultation.get("context_compressed"))
    task_type = consultation.get("task_type")
    mode = consultation.get("mode")
    created = consultation.get("created")
    consensus_response = _coerce_text(consultation.get("consensus_response")) or ""
    consensus_score = consultation.get("consensus_score")
    winning_muse = consultation.get("winning_muse")
    cost = consultation.get("cost")
    latency_ms = consultation.get("latency_ms")
    model_variants = _coerce_json(consultation.get("model_variants"))

    muses_quorum: list[dict[str, Any]] = []
    muses_full: list[dict[str, Any]] = []
    for row in audit_rows:
        provider = row.get("provider")
        model = row.get("model")
        quality_score = row.get("quality_score")
        row_latency_ms = row.get("latency_ms")
        muses_quorum.append(
            {
                "provider": provider,
                "model": model,
                "quality_score": quality_score,
                "latency_ms": row_latency_ms,
            }
        )
        muses_full.append(
            {
                "provider": provider,
                "model": model,
                "response_text": _coerce_text(row.get("response_text")) or "",
            }
        )

    # Verbatim synthesis: graeae_consultations.consensus_response was
    # historically capped at 500 chars on write, but the untruncated
    # consensus is preserved on the winning-muse graeae_audit_log row
    # (response_text). Prefer that whenever it is at least as long, so a
    # recall over the existing corpus returns the full synthesis.
    synthesis_text = consensus_response
    for row in audit_rows:
        if row.get("provider") == winning_muse:
            full = _coerce_text(row.get("response_text")) or ""
            if len(full) >= len(synthesis_text):
                synthesis_text = full
            break
    if not synthesis_text and audit_rows:
        synthesis_text = _coerce_text(audit_rows[0].get("response_text")) or ""

    return {
        "consultation_id": consultation_id,
        "source": {
            "prompt": prompt,
            "context": context,
            "task_type": task_type,
            "mode": mode,
            "created": created,
        },
        "quorum": {
            "consensus_score": consensus_score,
            "winning_muse": winning_muse,
            "cost": cost,
            "latency_ms": latency_ms,
            "model_variants": model_variants,
            "muses": muses_quorum,
        },
        "synthesis": {
            "text": synthesis_text,
        },
        "muses": muses_full,
    }


__all__ = [
    "JSONObject",
    "JSONScalar",
    "JSONValue",
    "Row",
    "MEMORY_COLS",
    "_MEMORY_COLS",
    "assemble_consultation_full",
    "_coerce_text",
    "_coerce_json",
    "ModelRecommendation",
]


@dataclass(frozen=True, slots=True)
class ModelRecommendation:
    """Backend-neutral shape for model routing recommendations."""

    provider: str
    model_id: str
    display_name: str | None
    cost_per_mtok: float
    quality_score: float
    context_window: int | None
