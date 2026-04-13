FROM python:3.11-slim

WORKDIR /app

# Install system deps for asyncpg + psycopg + numpy
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# For phi_server.py (Intel OpenVINO): pip install -r requirements-phi.txt
COPY requirements.txt .

# Install only the runtime deps needed for the API server
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PG_USER=mnemos_user \
    PG_DATABASE=mnemos \
    PG_HOST=postgres \
    OLLAMA_EMBED_HOST=http://ollama:11434 \
    MNEMOS_PORT=5002

EXPOSE 5002

CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "5002", "--workers", "1"]
