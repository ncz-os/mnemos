# Security Policy

## Supported versions

The most recently maintained branch is the supported branch.

## Reporting a vulnerability

Please do not open a public GitHub issue for suspected vulnerabilities.

Instead, report security issues privately via GitHub: **@mnemos-dev** or by email to **security@mnemos.dev** (configure this address before public release)

Please include:
- a description of the issue
- impact assessment
- reproduction steps
- any suggested remediation

If a dedicated disclosure channel is added later, this file should be updated.

## Secrets policy

- Never commit `.env` files or live credentials.
- Store provider keys outside the repository.
- Sanitize infrastructure-specific details before public release.
