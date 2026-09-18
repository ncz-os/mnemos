"""Offline MPF → MIF 1.0 migration.

Converts an existing **MPF** archive (the legacy Memory Portability Format
envelope, or its JSONL form) into a **MIF 1.0 bundle** (a directory of concept
files + manifest), with no running MNEMOS server required. This is the one-time
migration path for archives produced before the MIF cut-over; live data is
migrated by re-exporting with ``--format mif``.

Usage:
    python -m mnemos.tools.mpf_to_mif --file memories.json  --out ./mif-bundle
    python -m mnemos.tools.mpf_to_mif --file memories.jsonl --out ./mif-bundle

By default this tool FAILS if the input carries any CHARON surface that
MIF 1.0 does not preserve — v0.2 record-level fields (provenance,
valid_time_*, transaction_time), KG triples, memory_versions,
compression_manifest / compression_candidates, embeddings, attestations,
relations, or deletion_log. These either become concept metadata or
cannot be represented in the MIF directory layout at all. Pass
``--drop-unsupported-sidecars`` to authorise the loss explicitly and
proceed with conversion (sidecars are dropped; v0.2 record fields are
discarded).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


# MPF envelope sidecar arrays that MIF 1.0 does not preserve.
# Keep in sync with MPFEnvelope in mnemos.domain.portability.schemas.
_UNSUPPORTED_SIDECARS = (
    "kg_triples",
    "relations",
    "memory_versions",
    "compression_manifest",
    "compression_candidates",
    "embeddings",
    "attestations",
    "deletion_log",
)

# MPF record-level v0.2 fields that are dropped on a default conversion
# because MIF concepts don't have first-class slots for them.
_UNSUPPORTED_V02_RECORD_FIELDS = (
    "provenance",
    "valid_time_start",
    "valid_time_end",
    "transaction_time",
)


def _memory_from_record(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Flatten one MPF record to a memory row (kind=='memory' only)."""
    if rec.get("kind") != "memory":
        return None
    payload = dict(rec.get("payload") or {})
    payload.setdefault("id", rec.get("id"))
    return payload


def _load_mpf_memories(path: Path) -> List[Dict[str, Any]]:
    """Read an MPF envelope (JSON) or JSONL file into a list of memory rows.

    The envelope / trailer object observed during the parse is stashed on
    the module-level ``_LAST_INPUT_SURFACES`` dict, keyed by the file's
    resolved path, so the follow-up ``_validate_mpf_mif_surfaces`` call
    in :func:`convert` can inspect sidecars + v0.2 record fields without
    re-parsing the file. Path objects are immutable so we cannot attach
    attributes to them; the module dict is the agreed handoff point.
    """
    text = path.read_text(encoding="utf-8").strip()
    resolved = str(path.resolve())
    surfaces: Dict[str, Any] = {"envelope": None, "trailer": None}
    memories: List[Dict[str, Any]] = []
    if path.suffix.lower() == ".jsonl" or (text and not text.lstrip().startswith("{")):
        # JSONL: one MPF record per line; a trailing {"mpf_sidecars": true,...}
        # trailer carries any populated sidecar arrays.
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("mpf_sidecars"):
                surfaces["trailer"] = obj
                continue
            mem = _memory_from_record(obj)
            if mem is not None:
                memories.append(mem)
    else:
        # Single MPF envelope object with a records[] array.
        envelope = json.loads(text)
        for rec in envelope.get("records") or []:
            mem = _memory_from_record(rec)
            if mem is not None:
                memories.append(mem)
        surfaces["envelope"] = envelope
    _LAST_INPUT_SURFACES[resolved] = surfaces
    return memories


# Per-resolved-path observation handoff between _load_mpf_memories and
# _validate_mpf_mif_surfaces. Keyed by str(path.resolve()) so a caller
# running the same parser twice on the same file gets a fresh entry
# without any cross-run leakage.
_LAST_INPUT_SURFACES: Dict[str, Dict[str, Any]] = {}


def _validate_mpf_mif_surfaces(path: Path, *, drop_unsupported_sidecars: bool) -> None:
    """Refuse conversion when the input carries CHARON surfaces that MIF
    cannot preserve, unless the operator has explicitly authorised the loss.

    Surfaces checked:
    - MPF envelope top-level sidecars (``kg_triples``, ``relations``,
      ``memory_versions``, ``compression_manifest``, ``compression_candidates``,
      ``embeddings``, ``attestations``, ``deletion_log``).
    - MPF v0.2 record-level fields (``provenance``, ``valid_time_start``,
      ``valid_time_end``, ``transaction_time``).
    - JSONL trailer ``mpf_sidecars=true`` line, if present.

    Raises ``RuntimeError`` listing the surfaces that would be lost.
    """
    lost: List[str] = []
    surfaces = _LAST_INPUT_SURFACES.get(str(path.resolve()), {})
    envelope = surfaces.get("envelope")
    if isinstance(envelope, dict):
        for k in _UNSUPPORTED_SIDECARS:
            arr = envelope.get(k)
            if isinstance(arr, list) and arr:
                lost.append(f"{k}={len(arr)}")
        for rec in envelope.get("records") or []:
            if not isinstance(rec, dict):
                continue
            for f in _UNSUPPORTED_V02_RECORD_FIELDS:
                if rec.get(f) is not None:
                    lost.append(f"v0.2 record field {f!r}")
                    # One report per record is enough.
                    break
    trailer = surfaces.get("trailer")
    if isinstance(trailer, dict):
        for k in _UNSUPPORTED_SIDECARS:
            arr = trailer.get(k)
            if isinstance(arr, list) and arr:
                lost.append(f"jsonl trailer {k}={len(arr)}")
    if lost and not drop_unsupported_sidecars:
        raise RuntimeError(
            "MPF→MIF conversion would lose CHARON surfaces that MIF 1.0 "
            "cannot preserve: " + ", ".join(sorted(set(lost))) + ". "
            "Re-run with --drop-unsupported-sidecars to authorise the "
            "loss, or migrate these surfaces via a live /v1/import round-trip "
            "instead of the offline MIF path."
        )


def convert(
    mpf_file: str,
    out_dir: str,
    *,
    redact_vault: bool = True,
    drop_unsupported_sidecars: bool = False,
) -> Dict[str, Any]:
    """Convert an MPF file to a MIF bundle; returns the bundle manifest."""
    from mnemos.portability import charon as mif_charon

    path = Path(mpf_file)
    memories = _load_mpf_memories(path)
    _validate_mpf_mif_surfaces(path, drop_unsupported_sidecars=drop_unsupported_sidecars)
    return mif_charon.export_bundle(memories, Path(out_dir), redact_vault=redact_vault)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="mpf_to_mif",
        description=(
            "Convert a legacy MPF envelope/JSONL file to a MIF 1.0 bundle. "
            "Refuses by default when the input carries CHARON sidecars or "
            "v0.2 record fields that MIF 1.0 cannot preserve; pass "
            "--drop-unsupported-sidecars to authorise their loss."
        ),
    )
    parser.add_argument("--file", required=True, metavar="PATH", help="MPF envelope (.json) or .jsonl file")
    parser.add_argument("--out", required=True, metavar="DIR", help="Output MIF bundle directory")
    parser.add_argument(
        "--include-vault",
        action="store_true",
        help="Emit vault (secret) content instead of redacting it (authorized migrations only).",
    )
    parser.add_argument(
        "--drop-unsupported-sidecars",
        action="store_true",
        help=(
            "Authorise the loss of CHARON sidecars and v0.2 record fields "
            "that MIF 1.0 cannot preserve. By default the tool refuses "
            "such inputs with a list of what would be dropped."
        ),
    )
    args = parser.parse_args(argv)
    try:
        manifest = convert(
            args.file,
            args.out,
            redact_vault=not args.include_vault,
            drop_unsupported_sidecars=args.drop_unsupported_sidecars,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
    print(f"Converted {manifest['count']} memories: MPF {args.file} → MIF {manifest['mif_version']} bundle {args.out}")


if __name__ == "__main__":
    sys.exit(main())
