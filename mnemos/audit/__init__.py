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
    AuditEntry,
    canonical_entry_bytes,
    canonical_payload_hash,
    derive_writer_keypair,
    entry_hash,
    load_root_keypair,
    merkle_leaf,
    merkle_root,
    sign_entry,
    verify_entry,
)
from .writer import build_entry, latest_hash

__all__ = [
    "AuditEntry",
    "build_entry",
    "canonical_entry_bytes",
    "canonical_payload_hash",
    "derive_writer_keypair",
    "entry_hash",
    "latest_hash",
    "load_root_keypair",
    "merkle_leaf",
    "merkle_root",
    "sign_entry",
    "verify_entry",
]
