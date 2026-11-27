# MNEMOS-OS Dockerfile — PostgreSQL-profile image.
# Multi-stage build with uv for fast dependency resolution (10x faster than pip)
#
# Backend coverage: this image bundles asyncpg + libpq5 only (PostgreSQL).
# To run MNEMOS against Oracle 23ai or IBM Db2 12.1.5 from a container, either:
#   - extend this image with the matching driver (oracledb thin-mode needs no
#     additional system libs; oracledb thick-mode + ibm_db both need vendor
#     client libraries), OR
#   - build from a separate `Dockerfile.enterprise` (not provided in this
#     branch — supply your own per docs/INSTALL.md "Enterprise Backends").
# Heavy Oracle/Db2 client installs are deliberately NOT baked in here to keep
# the default image small. Set MNEMOS_DATABASE_DSN at runtime to point at a
# vendor-distributed database container; see docker-compose.yml for the
# commented enterprise profile stubs.

# Stage 1: Builder with uv (fast dependency installation)
FROM python:3.13-slim as builder

WORKDIR /app

# Install system deps: build tools + asyncpg + psycopg + numpy + GPU detection
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev curl git && \
    rm -rf /var/lib/apt/lists/*

# Install a versioned uv installer and verify its bytes before execution.
ARG UV_INSTALLER_SHA256=505407eb4b5bec00e0b231c8c86e968ee3f5aa0136d49dd8a541179e7f76ce7d
RUN curl -fsSL -o /tmp/uv-install.sh https://astral.sh/uv/0.5.11/install.sh && \
    echo "${UV_INSTALLER_SHA256}  /tmp/uv-install.sh" | sha256sum -c - && \
    sh /tmp/uv-install.sh && rm /tmp/uv-install.sh
ENV PATH="/root/.local/bin:$PATH"

# Copy pyproject.toml (uv will handle dependency resolution)
COPY pyproject.toml .
COPY requirements.txt .

# Use uv to create a thin virtual environment with all deps
# uv pip install is ~10x faster than pip; --system needed because we're not in a venv
RUN uv pip install --system -r requirements.txt

# Stage 2: Runtime (minimal footprint)
FROM python:3.13-slim

WORKDIR /app

# Install only runtime system deps. libgomp1 is required by
# llama-cpp-python's libllama.so (OpenMP runtime); curl is needed to
# fetch the embedding GGUF in the layer below.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 libgomp1 curl && \
    rm -rf /var/lib/apt/lists/*

# Intel OpenCL ICD + IGC + Level Zero for Intel Xe iGPU OpenVINO embed.
# Without these the OpenVINO GPU plugin fails clBuildProgram with
# CL_OUT_OF_HOST_MEMORY (-6) on BERT-class models. Stale ICD shipped in
# Debian trixie / older base layers is the actual cause; pulling fresh
# packages from intel/compute-runtime + intel/intel-graphics-compiler
# GitHub releases unblocks bge-base-en-v1.5 at ~110 rec/s short / 18
# rec/s long on Intel Xe (vs 79 / 7 on the same CPU). See MNEMOS
# mem_1779375115090_a9fc6e + mem_1779376091245_b8aeed.
ARG INTEL_COMPUTE_RUNTIME=26.18.38308.1
ARG INTEL_IGC=2.34.4
ARG INTEL_IGC_BUILD=21428
RUN set -eux; \
    mkdir -p /tmp/intel-icd && cd /tmp/intel-icd; \
    BASE_CR="https://github.com/intel/compute-runtime/releases/download/${INTEL_COMPUTE_RUNTIME}"; \
    BASE_IGC="https://github.com/intel/intel-graphics-compiler/releases/download/v${INTEL_IGC}"; \
    curl -fsSLO "${BASE_CR}/intel-opencl-icd_${INTEL_COMPUTE_RUNTIME}-0_amd64.deb"; \
    curl -fsSLO "${BASE_CR}/intel-ocloc_${INTEL_COMPUTE_RUNTIME}-0_amd64.deb"; \
    curl -fsSLO "${BASE_CR}/libze-intel-gpu1_${INTEL_COMPUTE_RUNTIME}-0_amd64.deb"; \
    curl -fsSLO "${BASE_CR}/libigdgmm12_22.10.0_amd64.deb"; \
    curl -fsSLO "${BASE_IGC}/intel-igc-core-2_${INTEL_IGC}+${INTEL_IGC_BUILD}_amd64.deb"; \
    curl -fsSLO "${BASE_IGC}/intel-igc-opencl-2_${INTEL_IGC}+${INTEL_IGC_BUILD}_amd64.deb"; \
    printf '%s\n' \
      "0ce1e715ec5bf30b1304b42acfbda34cc514a0f616dd48c6a31973c4075d8d09  intel-igc-core-2_${INTEL_IGC}+${INTEL_IGC_BUILD}_amd64.deb" \
      "09a71e8b6ad432ed0511b09fa4f580c1b44534a60ccb0545212507bf67dc0d1b  intel-igc-opencl-2_${INTEL_IGC}+${INTEL_IGC_BUILD}_amd64.deb" \
      "f36cb8a6899353c61cc7261b650f287f4a652acadaad859103bdfc51f93b6e8a  intel-ocloc_${INTEL_COMPUTE_RUNTIME}-0_amd64.deb" \
      "b2d0c924e56b3f9e5837774d68b0c67461b8633035d93ca18b1a8e3e5ead15fa  intel-opencl-icd_${INTEL_COMPUTE_RUNTIME}-0_amd64.deb" \
      "6031a63d6e8a12ce61c14efc15f2c8e727061286e3820b8594e6d00615e04d54  libigdgmm12_22.10.0_amd64.deb" \
      "12b8254e6d3415c32cee9cd13943030b991d91212445c79fe1cc27176a72eca4  libze-intel-gpu1_${INTEL_COMPUTE_RUNTIME}-0_amd64.deb" \
      | sha256sum -c -; \
    apt-get update && apt-get install -y --no-install-recommends ocl-icd-libopencl1; \
    dpkg -i \
      intel-igc-core-2_${INTEL_IGC}+${INTEL_IGC_BUILD}_amd64.deb \
      intel-igc-opencl-2_${INTEL_IGC}+${INTEL_IGC_BUILD}_amd64.deb \
      intel-opencl-icd_${INTEL_COMPUTE_RUNTIME}-0_amd64.deb \
      intel-ocloc_${INTEL_COMPUTE_RUNTIME}-0_amd64.deb \
      libze-intel-gpu1_${INTEL_COMPUTE_RUNTIME}-0_amd64.deb \
      libigdgmm12_22.10.0_amd64.deb; \
    rm -rf /tmp/intel-icd /var/lib/apt/lists/*

# Copy installed packages from builder (preserves installation with all deps)
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages

# Copy application code
COPY . .

# setuptools is required by the PEP 517 build backend below and is not
# preinstalled in python:3.13-slim; --no-build-isolation means pip cannot
# fetch it itself at install time, so it must be present beforehand.
RUN python -m pip install --no-cache-dir setuptools

# Register the package itself so importlib.metadata.version("mnemos-os")
# matches pyproject.toml. --no-deps because deps are already installed
# from requirements.txt above.
RUN python -m pip install --no-deps --no-build-isolation .

# In-process embedding model. Architectural decision
# mem_1779334716543_f8ebd4 (operator-locked 2026-05-21): MNEMOS embed
# generation is in-process — no Ollama, no llama-server HTTP. The
# default GGUF is nomic-embed-text-v1.5.Q8_0 (768-dim), matching the
# existing `memories.embedding vector(768)` pgvector column. Override
# via MNEMOS_EMBED_MODEL_PATH at runtime (and bake an alternate model
# into the image at build time by replacing this layer) — but if the
# replacement model does not emit 768 dimensions, MNEMOS_EMBEDDING_DIM
# must be overridden to match it, or the vector column width will not
# match the embeddings being written.
ARG MNEMOS_EMBED_MODEL_SHA256=3e24342164b3d94991ba9692fdc0dd08e3fd7362e0aacc396a9a5c54a544c3b7
RUN mkdir -p /opt/mnemos/models && \
    curl -fsSL -o /opt/mnemos/models/nomic-embed-text-v1.5.Q8_0.gguf \
      https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF/resolve/main/nomic-embed-text-v1.5.Q8_0.gguf && \
    echo "${MNEMOS_EMBED_MODEL_SHA256}  /opt/mnemos/models/nomic-embed-text-v1.5.Q8_0.gguf" | sha256sum -c -

# Environment variables. MNEMOS_EMBED_OV_DEVICE defaults to AUTO so the
# in-process embedder picks GPU > NPU > CPU at startup. Operator overrides
# at runtime via compose env when locking to a specific device.
# Default config targets the PostgreSQL profile. Override at runtime with
# MNEMOS_DATABASE_DSN to select a different backend without rebuilding:
#   MNEMOS_DATABASE_DSN=postgres://user:pass@host:5432/dbname
#   MNEMOS_DATABASE_DSN=oracle://user:pass@host:1521/service_name
#   MNEMOS_DATABASE_DSN=db2://user:pass@host:50000/dbname
#   MNEMOS_DATABASE_DSN=sqlite:///data/mnemos.db
ENV PG_USER=mnemos_user \
    PG_DATABASE=mnemos \
    PG_HOST=postgres \
    MNEMOS_EMBEDDING_DIM=768 \
    MNEMOS_EMBED_MODEL_PATH=/opt/mnemos/models/nomic-embed-text-v1.5.Q8_0.gguf \
    MNEMOS_EMBED_OV_DEVICE=AUTO \
    OV_CACHE_DIR=/opt/mnemos/ov_cache \
    MNEMOS_BIND=0.0.0.0 \
    MNEMOS_PORT=5002 \
    PYTHONUNBUFFERED=1

RUN useradd --uid 10001 --gid 0 --no-create-home --home-dir /home/mnemos \
      --shell /usr/sbin/nologin mnemos && \
    mkdir -p /home/mnemos /data /opt/mnemos && \
    chown -R 10001:0 /app /home/mnemos /data /opt/mnemos && \
    chmod -R g=u /app /home/mnemos /data /opt/mnemos
USER 10001:0
ENV HOME=/home/mnemos

EXPOSE 5002

# Health check (optional)
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5002/health').read()" || exit 1

CMD ["mnemos", "serve"]
