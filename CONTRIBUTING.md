# Contributing

Thanks for your interest in MNEMOS.

## License

By contributing to this repository, you agree that your contribution may be
distributed under the repository's Apache License 2.0 terms and referenced in
the project's dual-license commercial offering.

## Development workflow

- Use a feature branch for non-trivial changes.
- Keep the upstream remote as the authoritative Git source, and use a local checkout for development and test runs.
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
