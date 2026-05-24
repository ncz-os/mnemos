"""v6.2 M-2.2.1 audit chain primitives.

Public surface:

    from mnemos.audit import (
        canonical_payload_hash,
        canonical_entry_bytes,
        derive_writer_keypair,
        load_root_keypair,
        sign_entry,
        verify_entry,
        merkle_root,
    )

Schemas shipped in db/migrations*/0029-0030 (commit 614d483).
Sealer worker + route wiring follow in later commits.
"""

from __future__ import annotations

from .crypto import (
    canonical_entry_bytes,
    canonical_payload_hash,
    derive_writer_keypair,
    load_root_keypair,
    merkle_root,
    sign_entry,
    verify_entry,
)

__all__ = [
    "canonical_entry_bytes",
    "canonical_payload_hash",
    "derive_writer_keypair",
    "load_root_keypair",
    "merkle_root",
    "sign_entry",
    "verify_entry",
]
