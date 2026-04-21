# MNEMOS API Documentation

**Base URL**: `http://localhost:5002`
**Version**: 3.0.0
**Format**: JSON

---

> **Note on v3 API surface:** primary routes are namespaced under `/v1/*` as of v3.0.0.
> Pre-v3 paths (`/memories`, `/memories/search`, `/graeae/consult`, `/model-registry/*`) remain functional
> as **deprecated aliases** for backward compatibility and will be removed in a future major version.
> This document describes the pre-v3 paths. For the v3 surface (including `/v1/consultations`,
> `/v1/providers`, `/v1/webhooks`, `/auth/oauth/*`, `/v1/federation/*`, and the OpenAI-compatible
> gateway at `/v1/chat/completions`), see `README.md`.
> A full v3-first rewrite of this document is tracked for a follow-up release.

---

## Table of Contents

1. [Authentication](#authentication)
2. [Health & Status](#health--status)
3. [Memory Operations](#memory-operations)
4. [Compression & Audit](#compression--audit)
5. [Graeae Consultation](#graeae-consultation)
6. [Hook Management](#hook-management)
7. [State Management](#state-management)
8. [Additional Endpoints](#additional-endpoints)
9. [Error Handling](#error-handling)
10. [Examples](#examples)

---

## Authentication

**Current**: Personal installs may run without auth. Team and enterprise installs support API-key authentication with row-level security.

---

## Health & Status

### GET /health

Check API server health

**Response** (200):
```json
{
  "status": "healthy",
  "timestamp": "2026-02-05T14:30:00.000Z",
  "database_connected": true,
  "version": "2.3.0"
}
```

**Example**:
```bash
curl -X GET http://localhost:5002/health
```

---

### GET /stats

Get system statistics

**Query Parameters**:
- None

**Response** (200):
```json
{
  "total_memories": 1243,
  "total_compressions": 1243,
  "average_compression_ratio": 0.57,
  "average_quality_rating": 92,
  "memories_by_category": {
    "facts": 500,
    "identity": 200,
    "preferences": 300,
    "projects": 243
  },
  "memories_by_task_type": {
    "reasoning": 600,
    "code_generation": 300,
    "architecture_design": 200,
    "other": 143
  },
  "unreviewed_compressions": 15,
  "timestamp": "2026-02-05T14:30:00.000Z"
}
```

**Example**:
```bash
curl -X GET http://localhost:5002/stats
```

---

## Memory Operations

### POST /memories

Create a new memory with automatic compression

**Request Body**:
```json
{
  "content": "Memory content here (required)",
  "category": "facts|identity|preferences|projects (required)",
  "task_type": "reasoning (optional, default: reasoning)",
  "metadata": {
    "key": "value"
  }
}
```

**Response** (201):
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "created",
  "compressed": true,
  "quality_rating": 92
}
```

**Example**:
```bash
curl -X POST http://localhost:5002/memories \
  -H "Content-Type: application/json" \
  -d '{
    "content": "User completed project X with 98% accuracy",
    "category": "facts",
    "task_type": "reasoning",
    "metadata": {"project": "MyProject", "date": "2026-02-05"}
  }'
```

---

### GET /memories/{memory_id}

Retrieve a memory by ID

**Path Parameters**:
- `memory_id` (required): UUID of memory

**Response** (200):
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "content": "Memory content (compressed)",
  "compressed_content": "Compressed version",
  "quality_rating": 92,
  "category": "facts",
  "task_type": "reasoning",
  "created_at": "2026-02-05T14:30:00.000Z",
  "metadata": {}
}
```

**Example**:
```bash
curl -X GET http://localhost:5002/memories/550e8400-e29b-41d4-a716-446655440000
```

---

### POST /memories/search

Search memories by semantic similarity

**Query Parameters**:
- `query` (required): Search text
- `limit` (optional): Max results (default: 10)
- `min_similarity` (optional): Minimum similarity score (default: 0.3)

**Response** (200):
```json
[
  {
    "id": "uuid-1",
    "content": "Similar memory content",
    "similarity_score": 0.92,
    "quality_rating": 88,
    "category": "facts"
  },
  {
    "id": "uuid-2",
    "content": "Another related memory",
    "similarity_score": 0.85,
    "quality_rating": 90,
    "category": "facts"
  }
]
```

**Example**:
```bash
curl -X POST "http://localhost:5002/memories/search?query=project%20completion&limit=5"
```

---

## Compression & Audit

> Note: The `/compress` trigger endpoint is internal. Use `GET /stats` for compression statistics.

---

## Graeae Consultation

### POST /graeae/consult

Consult Graeae for multi-LLM consensus

**Request Body**:
```json
{
  "prompt": "Design a microservices architecture for a restaurant inspection system",
  "task_type": "architecture_design (optional, default: reasoning)",
  "context": "Additional context for the prompt (optional)",
  "mode": "auto|local|external (optional, default: auto)",
  "muses": ["gpt-4o", "gemini-1.5-pro"] (optional, specific providers)
}
```

**Response** (200):
```json
{
  "consensus_response": "Recommended approach: Use event-driven microservices with...",
  "consensus_score": 87.5,
  "winning_muse": "gpt-4o",
  "winning_latency_ms": 3200,
  "cost": 0.04,
  "mode": "external",
  "task_type": "architecture_design",
  "timestamp": "2026-02-05T14:30:00.000Z"
}
```

**Response Codes**:
- `200`: Successful consultation
- `504`: All providers timed out, fallback response used
- `500`: Server error

**Example**:
```bash
curl -X POST http://localhost:5002/graeae/consult \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "How should I optimize database queries for millions of records?",
    "task_type": "architecture_design",
    "mode": "external"
  }'
```

---

## Hook Management

### GET /hooks

List all registered hooks

**Response** (200):
```json
{
  "session.start": [],
  "prompt.submit": [],
  "memory.write": [],
  "memory.read": [],
  "rehydration.start": [],
  "graeae.consult": []
}
```

---

### GET /hooks/history

Get hook execution history

**Query Parameters**:
- `limit` (optional): Max entries (default: 50)

**Response** (200):
```json
[
  {
    "event_type": "session.start",
    "timestamp": "2026-02-05T14:30:00.000Z",
    "context": {
      "session_id": "uuid"
    },
    "source": "api"
  }
]
```

---

### POST /hooks/{event}/trigger

Manually trigger a hook event

**Path Parameters**:
- `event` (required): Hook event name

**Request Body**:
```json
{
  "session_id": "uuid",
  "context_key": "value"
}
```

**Response** (200):
```json
{
  "event": "session.start",
  "triggered": true,
  "timestamp": "2026-02-05T14:30:00.000Z"
}
```

---

## State Management

The state API provides generic key-value storage. Keys and values are arbitrary JSON.

### GET /state

List all state keys.

**Response** (200):
```json
{"keys": [{"key": "identity", "updated": "2026-02-05T14:30:00", "version": 1}]}
```

**Example**:
```bash
curl http://localhost:5002/state
```

---

### GET /state/{key}

Get value for a state key.

**Response** (200):
```json
{"key": "identity", "value": {"name": "Alice Developer"}, "updated": "2026-02-05T14:30:00", "version": 1}
```

**Example**:
```bash
curl http://localhost:5002/state/identity
```

---

### PUT /state/{key}

Set value for a state key (upsert).

**Request Body**:
```json
{"value": {"name": "Alice Developer", "workspace": "MyProject"}}
```

**Response** (200): Updated key record.

**Example**:
```bash
curl -X PUT http://localhost:5002/state/identity \
  -H "Content-Type: application/json" \
  -d '{"value": {"name": "Alice Developer"}}'
```

---

### DELETE /state/{key}

Delete a state key.

**Response**: 204 No Content, or 404 if not found.

**Example**:
```bash
curl -X DELETE http://localhost:5002/state/workspace
```


---

## Additional Endpoints

### Journal

#### POST /journal

Create a journal entry.

**Request Body**: `{topic, content, metadata?}`

**Example**:
```bash
curl -X POST http://localhost:5002/journal \
  -H "Content-Type: application/json" \
  -d '{"topic": "architecture", "content": "Decided on event-driven approach"}'
```

---

#### GET /journal

List journal entries. Query params: `topic`, `date`, `search`, `limit`.

**Example**:
```bash
curl "http://localhost:5002/journal?topic=architecture&limit=10"
```

---

#### DELETE /journal/{entry_id}

Delete a journal entry.

**Example**:
```bash
curl -X DELETE http://localhost:5002/journal/entry-uuid
```

---

### Entities

#### POST /entities

Create an entity. `entity_type` must be one of: `person`, `project`, `concept`, `document`, `decision`, `event`.

**Request Body**: `{entity_type, name, description?, metadata?}`

**Example**:
```bash
curl -X POST http://localhost:5002/entities \
  -H "Content-Type: application/json" \
  -d '{"entity_type": "project", "name": "MyProject", "description": "Main project"}'
```

---

#### GET /entities

List or search entities. Query params: `entity_type`, `search`, `limit`.

**Example**:
```bash
curl "http://localhost:5002/entities?entity_type=project"
```

---

#### GET /entities/{id}

Get a single entity.

---

#### PATCH /entities/{id}

Update entity description or metadata.

**Request Body**: `{description?, metadata?}`

---

#### POST /entities/{id}/link

Link two entities bidirectionally.

**Request Body**: `{related_id: "uuid"}`

---

#### DELETE /entities/{id}

Delete an entity (also removes it from all related_entities arrays).

---

#### GET /entities/{id}/related

Get all entities linked to this one.

---

### Model Registry

#### GET /model-registry/

List all tracked models.

---

#### GET /model-registry/best

Get the best-performing model per provider (by ELO score).

---

#### GET /model-registry/providers

List all tracked providers.

---

### GRAEAE Audit

#### GET /graeae/audit

Retrieve GRAEAE audit log entries.

---

#### GET /graeae/audit/verify

Verify the cryptographic integrity of the GRAEAE audit chain. Returns pass/fail per entry.

---


---

## Error Handling

### Error Response Format

```json
{
  "detail": "Error message describing what went wrong",
  "error_code": "INVALID_PARAMETER",
  "timestamp": "2026-02-05T14:30:00.000Z"
}
```

### Common HTTP Status Codes

| Code | Meaning | Example |
|------|---------|---------|
| 200 | Success | Memory retrieved successfully |
| 201 | Created | Memory created successfully |
| 400 | Bad Request | Invalid JSON or missing required field |
| 404 | Not Found | Memory ID does not exist |
| 408 | Timeout | Request took too long |
| 500 | Server Error | Database connection failed |
| 503 | Service Unavailable | Database is down |

---

## Examples

### Complete Memory Workflow

```bash
#!/bin/bash

BASE_URL="http://localhost:5002"

# 1. Create memory
MEMORY_ID=$(curl -s -X POST $BASE_URL/memories \
  -H "Content-Type: application/json" \
  -d '{
    "content": "Completed architecture design review",
    "category": "facts",
    "task_type": "architecture_design"
  }' | jq -r '.id')

echo "Created memory: $MEMORY_ID"

# 2. Retrieve memory
curl -s -X GET $BASE_URL/memories/$MEMORY_ID | jq '.'

# 3. Search for related memories
curl -s -X POST "$BASE_URL/memories/search" \
  -H "Content-Type: application/json" \
  -d '{"query": "architecture review", "limit": 5}' | jq '.'
```

### Consultation Workflow

```bash
#!/bin/bash

BASE_URL="http://localhost:5002"

# Consult Graeae
curl -s -X POST $BASE_URL/graeae/consult \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Design REST API for restaurants",
    "task_type": "architecture_design",
    "mode": "external"
  }' | jq '.'
```

### Batch Memory Operations

```bash
#!/bin/bash

BASE_URL="http://localhost:5002"

# Create multiple memories
for i in {1..5}; do
  curl -s -X POST $BASE_URL/memories \
    -H "Content-Type: application/json" \
    -d "{
      \"content\": \"Memory $i content\",
      \"category\": \"facts\",
      \"task_type\": \"reasoning\"
    }" | jq '.id'
done

# Search memories
curl -s -X POST "$BASE_URL/memories/search?query=content&limit=10" | jq '.[] | .id'
```

---

## Rate Limiting

**Current**: No rate limiting (deploy behind API gateway for production)

**Recommended**: 1000 requests/minute per client IP

---

## Versioning

API versioning via URL prefix (future):
- `/v1/memories`
- `/v2/memories`

Current: All endpoints at root level

---

## Support

For issues or questions:
1. Check logs: `sudo journalctl -u mnemos -f`
2. Review API_DOCUMENTATION.md
3. Check DEPLOYMENT_GUIDE.md for troubleshooting
4. Test endpoints manually with curl

---

**API Documentation Version**: 2.3.0
**Last Updated**: February 5, 2026
