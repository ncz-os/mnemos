"""MNEMOS API Server v2.3.0 — thin entrypoint, routes in api/ package."""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.lifecycle import lifespan
from api.handlers.health import router as health_router
from api.handlers.graeae_routes import router as graeae_router
from api.handlers.memories import router as memories_router
from api.handlers.ingest import router as ingest_router
from api.handlers.kg import router as kg_router
from api.handlers.admin import router as admin_router
from api.handlers.versions import router as versions_router
from api.handlers.model_registry_routes import router as model_registry_router

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

app = FastAPI(title="MNEMOS API", version="2.3.0", lifespan=lifespan)

# CORS: set CORS_ORIGINS env var to restrict in production (comma-separated list).
# Defaults to "*" for local dev. Example: CORS_ORIGINS=https://app.example.com
_cors_origins_raw = os.getenv("CORS_ORIGINS", "*")
_cors_origins = [o.strip() for o in _cors_origins_raw.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=_cors_origins != ["*"],
)

app.include_router(health_router)
app.include_router(graeae_router)
app.include_router(memories_router)
app.include_router(ingest_router)
app.include_router(kg_router)
app.include_router(admin_router)
app.include_router(versions_router)
app.include_router(model_registry_router)

if __name__ == "__main__":
    import uvicorn
    # NOTE: multi-process workers share nothing — each gets its own DB pool (min_size=5,
    # max_size=20). With workers=4 that is up to 80 Postgres connections. Adjust
    # pool sizes in config.toml [database] or run behind gunicorn with --workers 1
    # if your Postgres max_connections is constrained.
    uvicorn.run(app, host="0.0.0.0", port=5000, workers=4)
