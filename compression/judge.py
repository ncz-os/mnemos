"""Judge-LLM fidelity scoring (v3.3 S-II).

Replaces engine self-reported ``quality_score`` values in the
compression contest with a judge-rated fidelity score. The judge is
a separate LLM call that compares the narrated form of each
compressed candidate against the root memory and returns a score in
[0, 1]. This is what lets APOLLO's LLM fallback (currently pinned
at 0.65 below the 0.70 quality floor) actually win contests on
fact-shaped content where its dense encoding preserves meaning
better than LETHE's extract.

Design

  * ``Judge`` ABC — single async ``score()`` method. Callers supply
    (original, candidate dense form, candidate narrated form,
    engine id). Return is a ``JudgeScore`` or ``None`` on failure.
  * ``LLMJudge`` — GPU-backed concrete judge. ANAMNESIS-pattern
    httpx scaffolding against ``GPU_PROVIDER_HOST``; GPUGuard
    integration for circuit-open short-circuit; JSON parse with
    strict shape checking; None on malformed output.
  * ``NullJudge`` — no-op for disabled / test paths. Used as the
    default when the contest runs without a configured judge.

Integration point is ``compression.contest.run_contest(judge=...)``:
when a judge is supplied, every surviving candidate gets its
``quality_score`` replaced by the judge's fidelity rating BEFORE
composite_score is computed. The engine's self-reported score is
preserved in the candidate's manifest under ``engine_quality_score``
for audit clarity. The judge's model_id is stamped on
``CompressionResult.judge_model`` so the audit trail records which
judge scored which candidate.

On judge failure (HTTP error, parse failure, circuit-open), the
candidate falls back to its engine self-reported score — the
contest never fails closed because the judge is down.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import httpx

from .gpu_guard import get_guard

logger = logging.getLogger(__name__)


# GPU provider endpoint (shared with ANAMNESIS / APOLLO fallback).
_GPU_PROVIDER_HOST = os.getenv("GPU_PROVIDER_HOST", "http://localhost")
_GPU_PROVIDER_PORT = os.getenv("GPU_PROVIDER_PORT", "8000")
_GPU_PROVIDER_TIMEOUT = float(os.getenv("GPU_PROVIDER_TIMEOUT", "30.0"))


@dataclass
class JudgeScore:
    """Output of Judge.score().

    fidelity is in [0, 1]; 1.0 = perfect preservation of meaning,
    0.0 = compressed form unrelated to original. model_id records
    which model produced the score (stamped onto
    CompressionResult.judge_model for the audit log). reasoning is
    a short free-text justification the judge emits alongside the
    score; persisted into the candidate manifest but not used for
    scoring arithmetic.
    """

    fidelity: float
    model_id: str
    reasoning: str = ""


class Judge(ABC):
    """Base class for fidelity judges."""

    model_id: str = ""

    @abstractmethod
    async def score(
        self,
        *,
        original: str,
        candidate_encoded: str,
        candidate_narrated: str,
        candidate_engine_id: str,
    ) -> Optional[JudgeScore]:
        """Return a fidelity score [0, 1] for candidate against
        original. ``None`` signals judge failure — callers fall back
        to the engine's self-reported quality score; the contest
        never fails closed because the judge is down.

        Implementations MUST:
          * Not raise on infrastructure failures (return None, log).
          * Clamp any numeric output to [0, 1].
          * Stamp ``model_id`` on every returned JudgeScore.
        """
        raise NotImplementedError


class NullJudge(Judge):
    """No-op judge. Returns None for every candidate so the contest
    keeps using engine self-reported scores. Used as the default
    when ``MNEMOS_JUDGE_ENABLED`` is off."""

    model_id = "null"

    async def score(self, **kwargs) -> Optional[JudgeScore]:  # noqa: ARG002
        return None


# Strict shape for the judge's one-line JSON output.
# { "fidelity": 0.85, "reasoning": "brief" }
_JUDGE_OUTPUT_RE = re.compile(r"\{[^{}]*?\"fidelity\"[^{}]*?\}", re.DOTALL)


_JUDGE_PROMPT = """\
You are rating how faithfully a compressed memory preserves the meaning of the original.

Original memory:
{original}

Compressed memory (rendered back to prose for comparison):
{narrated}

Rate fidelity on a 0.0 to 1.0 scale:
  1.0 — all facts, identifiers, numbers, and nuance preserved
  0.8 — most content preserved, minor losses
  0.5 — partial preservation, some meaningful content lost
  0.2 — major content lost or distorted
  0.0 — compressed form does not reflect the original

Output ONE line of valid JSON, exactly this shape, no prose around it:
{{"fidelity": <float>, "reasoning": "<one-sentence justification>"}}

Output:"""


class LLMJudge(Judge):
    """GPU-backed fidelity judge.

    Uses the same ``GPU_PROVIDER_HOST`` endpoint as ANAMNESIS and
    APOLLO's LLM fallback; a single GPU host serves all three. The
    circuit breaker is shared — one open-circuit signal skips the
    judge alongside the other GPU-consuming engines, so a GPU
    outage degrades the contest to engine self-reported scoring
    rather than falling over entirely.
    """

    def __init__(
        self,
        model_id: str = "judge-default",
        gpu_url: Optional[str] = None,
        timeout: float = _GPU_PROVIDER_TIMEOUT,
    ) -> None:
        self.model_id = model_id
        if gpu_url:
            self.gpu_url = gpu_url.rstrip("/")
        else:
            host = _GPU_PROVIDER_HOST.rstrip("/")
            port = _GPU_PROVIDER_PORT
            self.gpu_url = f"{host}:{port}"
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def score(
        self,
        *,
        original: str,
        candidate_encoded: str,  # noqa: ARG002 — part of the Judge ABC
        candidate_narrated: str,
        candidate_engine_id: str,
    ) -> Optional[JudgeScore]:
        if not original or not candidate_narrated:
            return None

        guard = get_guard(self.gpu_url)
        admitted, probe_token = await guard.is_available()
        if not admitted:
            logger.info(
                "LLMJudge: circuit open for %s (%s); falling back to engine "
                "self-reported score for candidate engine=%s",
                self.gpu_url, guard.state.value, candidate_engine_id,
            )
            return None

        started = time.perf_counter()
        prompt = _JUDGE_PROMPT.format(
            original=original[:4000],
            narrated=candidate_narrated[:4000],
        )
        try:
            client = await self._get_client()
            response = await client.post(
                f"{self.gpu_url}/v1/completions",
                json={
                    "prompt": prompt,
                    "max_tokens": 200,
                    "temperature": 0.0,  # deterministic scoring
                    "top_p": 1.0,
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
            raw = (
                payload.get("choices", [{}])[0].get("text", "")
                if isinstance(payload, dict) else ""
            ).strip()
        except Exception as exc:
            logger.warning(
                "LLMJudge: HTTP call failed for candidate engine=%s: %s",
                candidate_engine_id, exc,
            )
            await guard.record_failure(exc, probe_token=probe_token)
            return None

        # HTTP 2xx received — signal success to the guard regardless
        # of whether the output parses (parse failure is a
        # prompt/model issue, not a GPU-health issue).
        await guard.record_success(probe_token=probe_token)

        parsed = _parse_judge_output(raw)
        if parsed is None:
            logger.warning(
                "LLMJudge: output parse failed for candidate engine=%s; "
                "raw=%r (first 200 chars)",
                candidate_engine_id, raw[:200],
            )
            return None

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.debug(
            "LLMJudge: scored candidate engine=%s fidelity=%.3f in %dms",
            candidate_engine_id, parsed.fidelity, elapsed_ms,
        )
        return JudgeScore(
            fidelity=parsed.fidelity,
            model_id=self.model_id,
            reasoning=parsed.reasoning,
        )


def _parse_judge_output(raw: str) -> Optional[JudgeScore]:
    """Parse a judge's one-line JSON output. Accepts preamble/suffix
    by extracting the first JSON object that contains a "fidelity"
    key. Clamps fidelity into [0, 1]."""
    if not raw:
        return None
    match = _JUDGE_OUTPUT_RE.search(raw)
    if match is None:
        return None
    try:
        obj = json.loads(match.group(0))
    except (ValueError, json.JSONDecodeError):
        return None
    fidelity = obj.get("fidelity")
    if not isinstance(fidelity, (int, float)):
        return None
    # Clamp to [0, 1] — a judge that returns 1.5 or -0.2 is honest
    # in intent but out-of-range for the contest's scoring math.
    clamped = max(0.0, min(1.0, float(fidelity)))
    reasoning = obj.get("reasoning")
    if not isinstance(reasoning, str):
        reasoning = ""
    # model_id is stamped by the caller (LLMJudge knows its own id);
    # _parse_judge_output returns a bare JudgeScore for the parsing
    # layer only. The Judge implementation re-wraps with its model_id.
    return JudgeScore(fidelity=clamped, model_id="", reasoning=reasoning[:500])
