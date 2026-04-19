"""MNEMOS API Server v2.3.0 — thin entrypoint, routes in api/ package."""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from api.rate_limit import (
    limiter,
    SlowAPIMiddleware,
    RateLimitExceeded,
    _rate_limit_exceeded_handler,
)

from api.lifecycle import lifespan
from api.handlers.health import router as health_router
from api.handlers.graeae_routes import router as graeae_router
from api.handlers.memories import router as memories_router
from api.handlers.ingest import router as ingest_router
from api.handlers.kg import router as kg_router
from api.handlers.admin import router as admin_router
from api.handlers.versions import router as versions_router
from api.handlers.model_registry_routes import router as model_registry_router
from api.handlers.journal import router as journal_router
from api.handlers.state import router as state_router
from api.handlers.entities import router as entities_router
from api.handlers.openai_compat import router as openai_compat_router
from api.handlers.sessions import router as sessions_router

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

app = FastAPI(title="MNEMOS API", version="2.3.0", lifespan=lifespan)

# ── Request body size limit (SEC-04) ──────────────────────────────────────────
# Default 5 MB. Override via MAX_BODY_BYTES env var.
_MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", str(5 * 1024 * 1024)))


class _BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Content-Length exceeds MAX_BODY_BYTES."""
    async def dispatch(self, request: Request, call_next):
        if request.method in ("POST", "PATCH", "PUT"):
            cl = request.headers.get("content-length")
            if cl and int(cl) > _MAX_BODY_BYTES:
                return JSONResponse(
                    {"detail": f"Request body exceeds {_MAX_BODY_BYTES // 1024 // 1024} MB limit"},
                    status_code=413,
                )
        return await call_next(request)


app.add_middleware(_BodySizeLimitMiddleware)

# Rate limiting (opt-in via RATE_LIMIT_ENABLED=true — see api/rate_limit.py)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# CORS: set CORS_ORIGINS env var to restrict in production (comma-separated list).
# Defaults to "*" for local dev. Example: CORS_ORIGINS=https://app.example.com
_cors_origins_raw = os.getenv("CORS_ORIGINS", "http://localhost,http://127.0.0.1,http://127.0.0.1:5002,http://localhost:5002")
_cors_origins = [o.strip() for o in _cors_origins_raw.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=_cors_origins != ["*"],
)

app.include_router(health_router)
app.include_router(openai_compat_router)  # Phase 0: OpenAI-compatible gateway
app.include_router(sessions_router)  # Phase 0: Session management for stateful chat
app.include_router(graeae_router)
app.include_router(memories_router)
app.include_router(ingest_router)
app.include_router(kg_router)
app.include_router(admin_router)
app.include_router(versions_router)
app.include_router(model_registry_router)
app.include_router(journal_router)
app.include_router(state_router)
app.include_router(entities_router)

if __name__ == "__main__":
    import uvicorn
    # workers=1 is required: GRAEAE circuit breakers, rate limiters, and semaphores
    # are in-process state. Multiple workers each get their own copy and will not
    # share limits. Use MNEMOS_PORT env var to override (default: 5002).
    port = int(os.getenv("MNEMOS_PORT", "5002"))
    host = os.getenv("MNEMOS_BIND", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, workers=1)
