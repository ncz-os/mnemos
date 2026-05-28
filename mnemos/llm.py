"""Thin KNEMON LLM wrapper with mandatory usage-ledger recording."""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

import mnemos.core.lifecycle as lifecycle
from mnemos.domain.graeae.engine import get_graeae_engine
from mnemos.domain.pantheon import triage
from mnemos.persistence.base import UsageLedgerRecord

logger = logging.getLogger(__name__)


class Task(BaseModel):
    prompt: str = ""
    messages: list[dict[str, Any]] | None = None
    task_kind: str = "chat"
    priority: str = "standard"
    ctx_size: int = Field(default=0, ge=0)
    quality_need: Literal["low", "med", "medium", "high"] = "med"
    caller_subsystem: str = "pantheon"
    tier: str = "standard"
    timeout: int = 180
    generation_params: dict[str, Any] | None = None
    request_params: dict[str, Any] | None = None


def _messages_prompt(task: Task) -> str:
    if task.prompt:
        return task.prompt
    parts: list[str] = []
    for message in task.messages or []:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
    return "\n".join(parts)


def _usage_value(result: Any, key: str) -> int:
    if not isinstance(result, dict):
        return 0
    usage = result.get("usage")
    if isinstance(usage, dict):
        aliases = {
            "tokens_in": ("tokens_in", "prompt_tokens", "input_tokens"),
            "tokens_out": ("tokens_out", "completion_tokens", "output_tokens"),
            "tokens_reasoning": ("tokens_reasoning", "reasoning_tokens"),
        }[key]
        for alias in aliases:
            if usage.get(alias) is not None:
                return max(0, int(usage[alias]))
    value = result.get(key)
    return max(0, int(value or 0))


async def _record_usage(record: UsageLedgerRecord) -> None:
    backend = lifecycle._persistence_backend
    if backend is None:
        raise RuntimeError("usage_ledger requires a configured persistence backend")
    recorder = getattr(backend, "record_usage_ledger", None)
    if recorder is None:
        raise RuntimeError("usage_ledger requires a configured persistence backend")
    async with backend.transactional() as tx:
        await recorder(tx, record)


async def call(task: Task | dict[str, Any]) -> dict[str, Any]:
    """Route one task through PANTHEON triage and always record usage."""
    request = task if isinstance(task, Task) else Task(**task)
    selected = await triage.recommend(
        request.task_kind,
        request.priority,
        request.ctx_size,
        request.quality_need,
    )
    provider = str(selected["provider"])
    model = str(selected["id"])
    started = time.monotonic()
    result: dict[str, Any] | None = None
    outcome = "err"
    try:
        result = await get_graeae_engine().route(
            provider,
            model,
            _messages_prompt(request),
            task_type=request.task_kind,
            timeout=request.timeout,
            generation_params=request.generation_params,
            request_params=request.request_params,
            messages=request.messages,
        )
        outcome = "ok" if result.get("status") == "success" else "err"
        return result
    except TimeoutError:
        outcome = "timeout"
        raise
    finally:
        latency_ms = int((time.monotonic() - started) * 1000)
        if isinstance(result, dict) and result.get("latency_ms") is not None:
            latency_ms = max(0, int(result["latency_ms"]))
        try:
            await _record_usage(
                UsageLedgerRecord(
                    provider=provider,
                    model=model,
                    task_kind=request.task_kind,
                    tokens_in=_usage_value(result, "tokens_in"),
                    tokens_out=_usage_value(result, "tokens_out"),
                    tokens_reasoning=_usage_value(result, "tokens_reasoning"),
                    latency_ms=latency_ms,
                    outcome=outcome,
                    caller_subsystem=request.caller_subsystem,
                    tier=request.tier,
                )
            )
        except Exception:
            logger.warning(
                "usage_ledger recording failed for provider=%s model=%s task_kind=%s",
                provider,
                model,
                request.task_kind,
                exc_info=True,
            )


__all__ = ["Task", "call"]
