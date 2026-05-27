"""mnemos/pantheon — knemon routing wrapper (resolves model via PANTHEON)."""

from __future__ import annotations

import asyncio

from mnemos.domain.pantheon.router import route_model


def route(task: str) -> str:
    """Resolve *task* to a model identifier via the PANTHEON auto:cheap alias.

    Returns the selected ``model_id`` string (e.g. ``"gpt-4o"``).
    """
    decision = asyncio.run(route_model("auto:cheap"))
    return decision.model_id or decision.alias
