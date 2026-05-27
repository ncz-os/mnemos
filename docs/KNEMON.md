# KNEMON — LLM Wrapper Layer

KNEMON is the mnemos LLM wrapper that wires together the three core subsystems:

```
call(task)
  ├── pantheon.route(task)          → resolves best model
  ├── providers.registry.invoke()   → calls the LLM provider  
  └── ledger.record()               → persists invocation record (always)
```

## Architecture

### `mnemos.pantheon.route(task: str) -> str`
Thin sync wrapper over the PANTHEON domain router. Uses `auto:cheap` alias
to pick the cheapest available model from the GRAEAE model registry.

### `mnemos.providers.registry.invoke(model: str, task: str) -> str`
Routes the task through the PANTHEON gateway to the actual LLM provider.
Returns the response text. On any error, returns an error description string
instead of raising — callers can check for error patterns.

### `mnemos.ledger.record(model: str, task: str, result: str | None) -> None`
Persists each invocation to the `memories` table (Postgres) under category
`knemon_invocation`. Always fires in a `finally` block so even failed calls
are recorded. Never raises.

## Usage

```python
from mnemos.llm import call

# Simple task — KNEMON handles routing, invocation, and recording
response = call("Write a Python function that sorts a list of dictionaries by key")
print(response)
```

## Configuration

KNEMON inherits configuration from:
- **PANTHEON**: `config.toml` → `[pantheon]` section (routing windows, weights, quality floors)
- **GRAEAE**: Provider registry with API keys, models, and capabilities
- **Postgres**: Standard mnemos persistence configuration

## Error Handling

- `pantheon.route()` raises if no models are available
- `providers.registry.invoke()` returns error strings on failure (never raises)
- `ledger.record()` swallows all errors silently
- `llm.call()` propagates provider errors after recording them

## Testing

```bash
# Run KNEMON wire integration tests
pytest tests/integration/test_knemon_wire.py -v

# Run with Postgres backend
MNEMOS_DB_BACKEND=postgres pytest tests/integration/test_knemon_wire.py -v
```

## Dependencies

- `mnemos.domain.pantheon` — model catalog, routing policy, gateway
- `mnemos.domain.graeae` — provider engine and API key management
- `mnemos.core.lifecycle` — Postgres connection pool
- `mnemos.persistence.postgres` — persistence layer

## See Also

- [PANTHEON Documentation](PANTHEON.md)
- [GRAEAE Documentation](GRAEAE.md)
- [API Documentation](../API_DOCUMENTATION.md)
