"""App-level encryption for at-rest OAuth provider client secrets.

GRAEAE consultation (2026-06-13) + vendor best practice: never store an OAuth
client_secret in plaintext. Vendor transparent encryption (Oracle TDE / DB2
native / MySQL keyring) only encrypts the files on disk and is transparent to
any DB user or SQL-injection path, so it still returns plaintext. Encrypt at the
application layer (Fernet / AES) and decrypt only transiently when
core.oauth.build_client needs the secret.

Key source: env ``MNEMOS_OAUTH_PROVIDER_KEY`` -- a urlsafe-base64 Fernet key, or
any passphrase (SHA-256-derived into a key). Fail-closed: an absent key raises
rather than silently storing or exposing a plaintext secret. Rotate by
re-encrypting provider rows under a new key (operator task).
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet

_ENV_KEY = "MNEMOS_OAUTH_PROVIDER_KEY"


def _fernet() -> Fernet:
    raw = os.environ.get(_ENV_KEY)
    if not raw:
        raise RuntimeError(
            f"{_ENV_KEY} is not set: an OAuth provider client_secret encryption key "
            "is required. Provider secrets are stored encrypted at rest and cannot be "
            "read or written without it (fail-closed; secrets are never stored plaintext)."
        )
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError) as exc:
        # Require a real Fernet key; a plain-passphrase + single-SHA256 derivation
        # is a weak KDF and gives false assurance. Fail closed with guidance.
        raise RuntimeError(
            f"{_ENV_KEY} is not a valid Fernet key. Generate one with: "
            "python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'  (a plain passphrase is not accepted)."
        ) from exc


def encrypt_provider_secret(plaintext: str) -> str:
    """Encrypt an OAuth client_secret for storage at rest."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_provider_secret(token: str) -> str:
    """Decrypt a stored OAuth client_secret (transient; for build_client only)."""
    return _fernet().decrypt(token.encode()).decode()
