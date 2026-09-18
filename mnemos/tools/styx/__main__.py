"""STYX command line — ``python -m mnemos.tools.styx``.

Subcommands:

  backup    Run one full backup (the systemd timer's ExecStart).
  keygen    Generate the age keypair for the key ceremony.
  verify    Report what STYX currently holds on Drive for this host.

Exit codes are the interface systemd consumes: 0 only on a fully verified
backup, 1 on any failure. There is deliberately no partial-success code.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from mnemos.tools.styx.config import (
    DEST_GDRIVE,
    DEST_R2,
    DEST_RCLONE,
    StyxConfig,
)
from mnemos.tools.styx.destination import build_destination
from mnemos.tools.styx.errors import StyxError
from mnemos.tools.styx.retention import plan_retention
from mnemos.tools.styx.runner import run_backup


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _cmd_keygen(_args: argparse.Namespace) -> int:
    """Generate an age keypair for the STYX key ceremony."""
    try:
        import pyrage
    except ImportError:
        print(
            "pyrage is not installed. Reinstall mnemos-core with its base dependencies",
            file=sys.stderr,
        )
        return 1

    identity = pyrage.x25519.Identity.generate()
    recipient = identity.to_public()
    print("STYX age keypair — generate this OFF the fleet, ideally on an air-gapped machine.\n")
    print("PRIVATE IDENTITY (the only thing that can ever decrypt a STYX backup).")
    print("Store it in at least two physically separate places the fleet cannot reach.")
    print("It must NEVER be written to a fleet host, a repo, or Drive:\n")
    print(f"  {identity}\n")
    print("PUBLIC RECIPIENT (safe to commit, safe to deploy to every host):\n")
    print(f"  MNEMOS_STYX_AGE_RECIPIENT={recipient}\n")
    print("Restore later with:  age -d -i <identity-file> backup.mif.tar.gz.age | tar xzf -")
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    config = StyxConfig.from_env()
    drive = None
    if not config.dry_run:
        drive = build_destination(config)

    receipt = asyncio.run(
        run_backup(
            config,
            drive=drive,
            allow_remote_http=args.allow_remote_http,
            keep_workdir=args.keep_workdir,
        )
    )
    print(json.dumps(receipt.to_dict(), indent=2))
    return 0 if receipt.ok else 1


def _destination_locator(config: StyxConfig) -> dict[str, str]:
    """Render the destination this run is targeting, in a shape the operator
    can read.

    The point of ``verify`` is to confirm "where are my backups going" —
    silent verification against an unexpected bucket, prefix, or remote is
    exactly the failure mode this command exists to prevent. The output is
    shaped per destination so the same command works regardless of which
    transport STYX was configured for.
    """
    kind = config.destination_kind
    if kind == DEST_GDRIVE:
        return {
            "kind": DEST_GDRIVE,
            "folder_id": config.gdrive_folder_id,
        }
    if kind == DEST_R2:
        bucket = config.r2_bucket or ""
        prefix = (config.r2_prefix or "").strip("/")
        locator = f"r2://{bucket}"
        if prefix:
            locator = f"{locator}/{prefix}"
        return {
            "kind": DEST_R2,
            "bucket": bucket,
            "prefix": prefix,
            "locator": locator,
        }
    if kind == DEST_RCLONE:
        return {
            "kind": DEST_RCLONE,
            "remote": config.rclone_remote or "",
        }
    return {"kind": kind}


def _cmd_verify(_args: argparse.Namespace) -> int:
    config = StyxConfig.from_env()
    drive = build_destination(config)
    artifacts = drive.list_artifacts(config.host_label)
    keep, prune = plan_retention(
        artifacts,
        daily=config.retain_daily,
        weekly=config.retain_weekly,
        monthly=config.retain_monthly,
    )
    payload: dict = {
        "host_label": config.host_label,
        "destination": _destination_locator(config),
        "total": len(artifacts),
        "would_keep": [a.name for a in keep],
        "would_prune": [a.name for a in prune],
    }
    # Preserve the original top-level shape (folder_id) so an operator whose
    # tooling keys on it does not break the day STYX gains a second destination.
    if config.destination_kind == DEST_GDRIVE:
        payload["folder_id"] = config.gdrive_folder_id
    print(json.dumps(payload, indent=2))
    if not artifacts:
        print("no STYX artifacts found for this host", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mnemos.tools.styx", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="run one full backup")
    backup.add_argument(
        "--keep-workdir",
        action="store_true",
        help="leave the cleartext work directory in place for forensics (it contains secrets)",
    )
    backup.add_argument(
        "--allow-remote-http",
        action="store_true",
        help="permit a non-loopback http source endpoint (off by design; STYX is per-host)",
    )
    backup.set_defaults(func=_cmd_backup)

    sub.add_parser("keygen", help="generate the age keypair").set_defaults(func=_cmd_keygen)
    sub.add_parser("verify", help="show what Drive holds for this host").set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    try:
        return int(args.func(args))
    except StyxError as exc:
        print(f"STYX FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
