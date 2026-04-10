"""MNEMOS API Server v2.3.0 — thin entrypoint, routes in api/ package."""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

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

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

app = FastAPI(title="MNEMOS API", version="2.3.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

app.include_router(health_router)
app.include_router(graeae_router)
app.include_router(memories_router)
app.include_router(ingest_router)
app.include_router(kg_router)
app.include_router(admin_router)
app.include_router(versions_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000, workers=4)
