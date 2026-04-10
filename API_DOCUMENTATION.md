# MNEMOS API Documentation

**Base URL**: `http://localhost:5000`
**Version**: 2.3.0
**Format**: JSON

---

## Table of Contents

1. [Authentication](#authentication)
2. [Health & Status](#health--status)
3. [Memory Operations](#memory-operations)
4. [Compression & Audit](#compression--audit)
5. [Graeae Consultation](#graeae-consultation)
6. [Hook Management](#hook-management)
7. [State Management](#state-management)
8. [Bundles & Routing](#bundles--routing)
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
curl -X GET http://localhost:5000/health
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
curl -X GET http://localhost:5000/stats
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
curl -X POST http://localhost:5000/memories \
  -H "Content-Type: application/json" \
  -d '{
    "content": "User completed project X with 98% accuracy",
    "category": "facts",
    "task_type": "reasoning",
    "metadata": {"project": "RiskyEats", "date": "2026-02-05"}
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
curl -X GET http://localhost:5000/memories/550e8400-e29b-41d4-a716-446655440000
```

---

### GET /memories/{memory_id}/quality-check

Get memory with quality assessment

**Path Parameters**:
- `memory_id` (required): UUID of memory

**Response** (200):
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "content": "Compressed content",
  "quality_rating": 92,
  "quality_manifest": {
    "compression_id": "uuid",
    "what_was_removed": [
      "2 introductory sentences",
      "3 supporting examples"
    ],
    "what_was_preserved": [
      "Core reasoning",
      "All conclusions",
      "15/18 entities"
    ],
    "risk_factors": [
      "Missing examples may reduce convincingness"
    ],
    "safe_for": ["Quick decisions", "Pattern recognition"],
    "not_safe_for": ["Detailed review", "Security-critical"]
  },
  "original_available": true,
  "original_memory_id": "uuid-original",
  "compression_ratio": 0.57
}
```

**Example**:
```bash
curl -X GET http://localhost:5000/memories/550e8400-e29b-41d4-a716-446655440000/quality-check
```

---

### GET /memories/{memory_id}/original

Retrieve uncompressed original memory

**Path Parameters**:
- `memory_id` (required): UUID of memory

**Response** (200):
```json
{
  "id": "uuid-original",
  "content": "Full uncompressed content with all details",
  "compression_ratio": 0.57,
  "quality_rating": 92
}
```

**Example**:
```bash
curl -X GET http://localhost:5000/memories/550e8400-e29b-41d4-a716-446655440000/original
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
curl -X POST "http://localhost:5000/memories/search?query=project%20completion&limit=5"
```

---

## Compression & Audit

### GET /compression-log

Get compression audit trail

**Query Parameters**:
- `task_type` (optional): Filter by task type
- `reviewed` (optional): true/false for review status
- `limit` (optional): Max entries (default: 50)

**Response** (200):
```json
[
  {
    "id": "compression-uuid",
    "memory_id": "memory-uuid",
    "original_tokens": 2450,
    "compressed_tokens": 980,
    "compression_ratio": 0.40,
    "quality_rating": 92,
    "task_type": "reasoning",
    "compression_method": "extractive token filter",
    "created_at": "2026-02-05T14:30:00.000Z",
    "reviewed": false,
    "review_notes": null
  }
]
```

**Example**:
```bash
curl -X GET "http://localhost:5000/compression-log?task_type=reasoning&reviewed=false&limit=20"
```

---

### POST /memories/{memory_id}/quality-review

Mark compression as reviewed

**Path Parameters**:
- `memory_id` (required): UUID of memory

**Request Body**:
```json
{
  "approved": true,
  "notes": "Quality acceptable for this use case"
}
```

**Response** (200):
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "reviewed": true,
  "approved": true,
  "timestamp": "2026-02-05T14:30:00.000Z"
}
```

**Example**:
```bash
curl -X POST http://localhost:5000/memories/550e8400-e29b-41d4-a716-446655440000/quality-review \
  -H "Content-Type: application/json" \
  -d '{
    "approved": true,
    "notes": "Compression quality acceptable"
  }'
```

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
  "muses": ["gpt-5.2", "gemini-3-pro"] (optional, specific providers)
}
```

**Response** (200):
```json
{
  "consensus_response": "Recommended approach: Use event-driven microservices with...",
  "consensus_score": 87.5,
  "winning_muse": "gpt-5.2",
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
curl -X POST http://localhost:5000/graeae/consult \
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

### GET /state/identity

Get user identity state

**Response** (200):
```json
{
  "id": "user-123",
  "name": "Jason Perlow",
  "workspace": "RiskyEats",
  "metadata": {
    "role": "developer",
    "team": "platform"
  }
}
```

---

### GET /state/today

Get today's state

**Response** (200):
```json
{
  "date": "2026-02-05T00:00:00.000Z",
  "day_of_week": "Wednesday",
  "schedule": [
    {
      "time": "09:00",
      "event": "Team standup"
    },
    {
      "time": "14:00",
      "event": "Architecture review"
    }
  ],
  "events": []
}
```

---

### GET /state/workspace

Get workspace state

**Response** (200):
```json
{
  "id": "workspace-1",
  "name": "RiskyEats",
  "active_project": "dashboard-redesign",
  "projects": [
    "dashboard-redesign",
    "api-optimization",
    "mobile-app"
  ],
  "settings": {
    "timezone": "UTC",
    "language": "en"
  }
}
```

---

### POST /state/sync

Sync state from macrodata

**Request Body**:
```json
{
  "identity": {
    "id": "user-123",
    "name": "Jason Perlow"
  },
  "today": {
    "date": "2026-02-05"
  },
  "workspace": {
    "name": "RiskyEats",
    "active_project": "dashboard"
  }
}
```

**Response** (200):
```json
{
  "identity": true,
  "today": true,
  "workspace": true,
  "timestamp": "2026-02-05T14:30:00.000Z"
}
```

---

## Bundles & Routing

### GET /bundles

List all consultation bundles

**Response** (200):
```json
{
  "code_generation": {
    "description": "Code generation bundle",
    "models": {
      "xai_grok": "grok-4-code",
      "openai": "gpt-5.2",
      "together_ai": "meta/llama-3-70b"
    }
  },
  "architecture_design": {
    "description": "Architecture design bundle",
    "models": {
      "openai": "gpt-5.2",
      "google": "gemini-3-pro",
      "groq": "llama-3.3-70b"
    }
  }
}
```

---

### GET /bundles/{bundle_type}

Get specific bundle details

**Path Parameters**:
- `bundle_type` (required): Bundle name

**Response** (200):
```json
{
  "bundle_type": "architecture_design",
  "description": "Multi-model consensus for system architecture",
  "models": {
    "openai": "gpt-5.2",
    "google": "gemini-3-pro",
    "groq": "llama-3.3-70b"
  },
  "consensus_score": 87,
  "compression_ratio": 0.4,
  "tags": ["system_design", "microservices"]
}
```

---

### POST /bundle/recommend

Get bundle recommendation for task

**Request Body**:
```json
{
  "task_description": "I need to design a scalable API for handling restaurant data"
}
```

**Response** (200):
```json
{
  "detected_task_type": "api_design",
  "recommended_bundle": "api_design",
  "primary_model": "gpt-5.2",
  "secondary_model": "gemini-3-pro",
  "confidence": 0.92
}
```

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

BASE_URL="http://localhost:5000"

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

# 3. Check quality
curl -s -X GET $BASE_URL/memories/$MEMORY_ID/quality-check | jq '.quality_rating'

# 4. Get original if needed
curl -s -X GET $BASE_URL/memories/$MEMORY_ID/original | jq '.content'

# 5. Mark as reviewed
curl -s -X POST $BASE_URL/memories/$MEMORY_ID/quality-review \
  -H "Content-Type: application/json" \
  -d '{
    "approved": true,
    "notes": "Quality acceptable"
  }' | jq '.'
```

### Consultation Workflow

```bash
#!/bin/bash

BASE_URL="http://localhost:5000"

# 1. Get bundle recommendation
BUNDLE=$(curl -s -X POST $BASE_URL/bundle/recommend \
  -H "Content-Type: application/json" \
  -d '{
    "task_description": "Design REST API for restaurants"
  }' | jq '.recommended_bundle' -r)

echo "Recommended bundle: $BUNDLE"

# 2. Consult Graeae
curl -s -X POST $BASE_URL/graeae/consult \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Design REST API for restaurants",
    "task_type": "'$BUNDLE'",
    "mode": "external"
  }' | jq '.'
```

### Batch Memory Operations

```bash
#!/bin/bash

BASE_URL="http://localhost:5000"

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

**API Documentation Version**: 2.0.0
**Last Updated**: February 5, 2026
