# Contributing

Thanks for your interest in MNEMOS.

## Development workflow

- Use `launch-prep` or a feature branch for non-trivial changes.
- Keep ARGONAS as authoritative Git storage, and use a compute node such as PYTHIA for development and test runs.
- Run the default test suite before opening a PR:

```bash
pytest -q
```

## Guidelines

- Prefer small, reviewable commits.
- Do not commit secrets, `.env` files, logs, backups, or local infrastructure notes.
- Keep public docs generic and portable.
- Add or update tests when behavior changes.

## Reporting issues

Please include:
- what you expected
- what happened
- reproduction steps
- relevant logs or tracebacks
