"""Client-side encryption for STYX bundles.

``age`` (RFC-style, X25519 + ChaCha20-Poly1305) via :mod:`pyrage`, chosen
over the alternatives for reasons that are all operational rather than
cryptographic:

* **Not GPG.** GPG wants a keyring and, in most real deployments, an agent.
  Unattended systemd on a mixed Fedora/Debian arm64+x86_64 fleet is exactly
  where that goes wrong, and its CLI surface is full of ways to silently
  encrypt to the wrong key.
* **Not hand-rolled AES-GCM.** Restorability is the requirement that settles
  it. A bespoke container means the operator can only decrypt with the exact
  script that wrote it, years later, from a machine that may have nothing
  else. ``age`` is a published format with multiple independent
  implementations; ``age -d`` or ``rage -d`` reads these files with no STYX
  code present at all.
* **Asymmetric, never symmetric.** A passphrase would have to live on the
  fleet in order to encrypt, so a total fleet loss — the disaster the backup
  exists for — would take the only means of reading the backups with it.

:mod:`pyrage` is the Rust ``rage`` implementation behind Python bindings, so
the bytes it writes are ordinary ``age`` files. Nothing here is a bespoke
format.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from mnemos.tools.styx.errors import StyxConfigError, StyxError

#: First bytes of any well-formed age file. Used as a cheap structural
#: assertion that we uploaded ciphertext and not, say, a truncated tarball.
AGE_MAGIC = b"age-encryption.org/v1"

_HASH_CHUNK = 1024 * 1024


def _require_pyrage():
    try:
        import pyrage  # noqa: PLC0415 - optional dependency, resolved at call time
    except ImportError as exc:  # pragma: no cover - exercised by the import-guard test
        raise StyxConfigError(
            "STYX needs the 'pyrage' package to encrypt bundles. Reinstall mnemos-core with its base dependencies"
        ) from exc
    return pyrage


def encrypt_file(src: Path, dst: Path, recipient: str) -> Path:
    """Encrypt ``src`` to ``dst`` for the age ``recipient``. Returns ``dst``.

    Streams through pyrage's file API rather than reading the bundle into
    memory, so a multi-gigabyte export on a small edge host does not have to
    fit in RAM.
    """
    pyrage = _require_pyrage()
    try:
        parsed = pyrage.x25519.Recipient.from_str(recipient)
    except Exception as exc:
        raise StyxConfigError(f"not a usable age recipient: {recipient[:16]!r}...") from exc

    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        pyrage.encrypt_file(str(src), str(dst), [parsed])
    except Exception as exc:
        raise StyxError(f"age encryption failed for {src}: {exc}") from exc

    if not dst.exists() or dst.stat().st_size == 0:
        raise StyxError(f"age encryption produced no output at {dst}")
    return dst


def has_age_header(path: Path) -> bool:
    """True when ``path`` begins with the age format header."""
    with path.open("rb") as handle:
        return handle.read(len(AGE_MAGIC)) == AGE_MAGIC


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def md5_file(path: Path) -> str:
    """MD5 of ``path``.

    Not a security control — it exists solely to compare against Drive's
    server-computed ``md5Checksum`` field, which is the cheapest way to prove
    the bytes that landed are the bytes that were sent. Integrity against a
    malicious party is provided by age's AEAD, not by this.
    """
    digest = hashlib.md5()  # noqa: S324 - matched against Drive's md5Checksum, not a security control
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()
