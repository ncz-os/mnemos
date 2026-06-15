"""Shadow OpenAI-compatible PANTHEON app.

Run on :4101 during PANTHEON phase B. This module intentionally does not touch
VIP :4100 or Caddy; operators can launch it with:

    MNEMOS_PANTHEON_ENABLED=true uvicorn mnemos.api.pantheon_shadow:app --host 127.0.0.1 --port 4101
"""

from __future__ import annotations

from fastapi import FastAPI

from mnemos.api.routes.pantheon import openai_router, router as pantheon_router
from mnemos.core.config import get_settings

app = FastAPI(title="PANTHEON OpenAI-compatible shadow gateway", version="0.2-shadow")
app.include_router(openai_router)
app.include_router(pantheon_router)


@app.get("/health")
async def health() -> dict[str, object]:
    settings = get_settings().pantheon
    return {
        "status": "ok",
        "service": "pantheon-shadow",
        "shadow_port": settings.shadow_port,
        "vip_4100_untouched": True,
    }
