"""mnemos/providers — knemon provider invocation wrapper."""

from __future__ import annotations

import asyncio
import logging

from mnemos.domain.pantheon.gateway import PantheonGatewayError, forward_chat_completion
from mnemos.domain.pantheon.router import RouteDecision, route_model

logger = logging.getLogger(__name__)


class _ProviderRegistry:
    """Simple registry that invokes a model via the PANTHEON gateway."""

    def invoke(self, model: str, task: str) -> str:
        """Send *task* to *model* and return the response text.

        On any error, returns a descriptive error string instead of
        raising (per the knemon contract: never crash the caller).
        """
        try:
            decision = asyncio.run(route_model(model))
        except Exception as exc:
            return f"[knemon] route error: {exc}"

        body = {
            "messages": [{"role": "user", "content": task}],
        }

        try:
            response: dict = asyncio.run(forward_chat_completion(decision, body))
        except PantheonGatewayError as exc:
            return f"[knemon] gateway error (HTTP {exc.status_code}): {exc.message[:500]}"
        except Exception as exc:
            return f"[knemon] provider error: {exc}"

        try:
            return response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            logger.warning("[knemon] unexpected response shape: %s", exc)
            return "[knemon] unexpected response shape"


registry = _ProviderRegistry()
