"""STYX configuration — every knob, resolved from the environment in one place.

Centralising the environment reads keeps the rest of STYX pure and testable:
nothing below this module touches ``os.environ``, so tests construct a
``StyxConfig`` directly instead of mutating process state.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

from mnemos.tools.styx.errors import StyxConfigError

#: Environment variable carrying the Google service-account credential. It
#: holds EITHER the JSON document itself OR a path to a file containing it,
#: because systemd units on this fleet supply secrets both ways
#: (``Environment=`` for short values, ``LoadCredential=`` for files).
ENV_GDRIVE_CREDENTIALS = "MNEMOS_STYX_GDRIVE_CREDENTIALS_JSON"

#: Destination folder id on Drive. A service account has no "My Drive" of
#: its own worth writing to, so an explicit shared-folder id is required
#: rather than defaulted — a backup uploaded somewhere nobody looks is the
#: silent-failure mode again, wearing a different hat.
ENV_GDRIVE_FOLDER_ID = "MNEMOS_STYX_GDRIVE_FOLDER_ID"

#: The ``age`` recipient (``age1...``) the bundle is encrypted to. Public
#: half only. If this ever holds an ``AGE-SECRET-KEY-1...`` value, the
#: private key has leaked onto the fleet and the design has been defeated —
#: STYX rejects that outright.
ENV_AGE_RECIPIENT = "MNEMOS_STYX_AGE_RECIPIENT"

#: Which upload transport to use. Defaults to the original GCP
#: service-account Google Drive path so an already-deployed host's config
#: keeps working unchanged. ``r2`` and ``rclone`` exist because the
#: service-account ceremony is real setup overhead a personal/home-fleet
#: deployment does not need — see ``docs/STYX.md``.
ENV_DESTINATION = "MNEMOS_STYX_DESTINATION"
DEST_GDRIVE = "gdrive-service-account"
DEST_R2 = "r2"
DEST_RCLONE = "rclone"
_DESTINATION_KINDS = (DEST_GDRIVE, DEST_R2, DEST_RCLONE)

#: Cloudflare R2 (S3-compatible). A scoped API token is the whole setup —
#: no OAuth flow, no consent screen. See docs/STYX.md for how to create a
#: token and a bucket dedicated to STYX (never reuse an existing bucket
#: that also serves another purpose).
ENV_R2_ACCOUNT_ID = "MNEMOS_STYX_R2_ACCOUNT_ID"
ENV_R2_BUCKET = "MNEMOS_STYX_R2_BUCKET"
ENV_R2_ACCESS_KEY_ID = "MNEMOS_STYX_R2_ACCESS_KEY_ID"
ENV_R2_SECRET_ACCESS_KEY = "MNEMOS_STYX_R2_SECRET_ACCESS_KEY"
ENV_R2_PREFIX = "MNEMOS_STYX_R2_PREFIX"

#: A named rclone remote (``remote:path``), configured entirely outside
#: this codebase via ``rclone config``. This process never holds a Drive
#: credential when this destination is selected — rclone owns that.
ENV_RCLONE_REMOTE = "MNEMOS_STYX_RCLONE_REMOTE"

ENV_HOST_LABEL = "MNEMOS_STYX_HOST_LABEL"
ENV_WORK_DIR = "MNEMOS_STYX_WORK_DIR"
ENV_SOURCE_MODE = "MNEMOS_STYX_SOURCE_MODE"
ENV_SQLITE_PATH = "MNEMOS_STYX_SQLITE_PATH"
ENV_HTTP_ENDPOINT = "MNEMOS_STYX_HTTP_ENDPOINT"
ENV_HTTP_TOKEN = "MNEMOS_STYX_HTTP_TOKEN"
ENV_MIN_RECORDS = "MNEMOS_STYX_MIN_RECORDS"
ENV_MIN_BYTES = "MNEMOS_STYX_MIN_BYTES"
ENV_RETAIN_DAILY = "MNEMOS_STYX_RETAIN_DAILY"
ENV_RETAIN_WEEKLY = "MNEMOS_STYX_RETAIN_WEEKLY"
ENV_RETAIN_MONTHLY = "MNEMOS_STYX_RETAIN_MONTHLY"
ENV_DRY_RUN = "MNEMOS_STYX_DRY_RUN"

#: Grandfather-father-son defaults. Two weeks of dailies covers "we broke it
#: on Friday and noticed on Monday"; eight weeklies cover a two-month
#: forensic window; twelve monthlies cover a year. At a few MB per encrypted
#: bundle the whole retained set is a rounding error against any Drive quota,
#: so the numbers are chosen for recovery latency rather than storage.
DEFAULT_RETAIN_DAILY = 14
DEFAULT_RETAIN_WEEKLY = 8
DEFAULT_RETAIN_MONTHLY = 12

#: A MIF bundle with zero records is never a legitimate backup.
DEFAULT_MIN_RECORDS = 1

#: Floor for the encrypted artifact. Deliberately small: an edge instance
#: genuinely can hold very few memories, and a floor tuned for the
#: authoritative host would fail those runs forever. The load-bearing
#: emptiness check is ``min_records``; this only catches a truncated or
#: zero-byte write.
DEFAULT_MIN_BYTES = 256

SOURCE_BACKEND = "backend"
SOURCE_HTTP = "http"
_SOURCE_MODES = (SOURCE_BACKEND, SOURCE_HTTP)

_AGE_RECIPIENT_PREFIX = "age1"
_AGE_IDENTITY_PREFIX = "AGE-SECRET-KEY-1"


def _int_env(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise StyxConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 0:
        raise StyxConfigError(f"{name} must not be negative, got {value}")
    return value


def _bool_env(env: dict[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class StyxConfig:
    """Everything a STYX run needs, validated up front."""

    gdrive_folder_id: str = ""
    age_recipient: str = ""
    # NB: every credential-bearing field is marked ``repr=False`` so that the
    # default frozen-dataclass ``__repr__`` (and therefore ``str(config)``) does
    # NOT echo it. ``runner.py`` / ``__main__.py`` / anyone who logs a config
    # in a traceback must never accidentally exfiltrate a service-account
    # JSON, an R2 access key, or the loopback HTTP token. Adding a new
    # credential field means marking it ``repr=False`` too.
    gdrive_credentials: str | None = field(default=None, repr=False)
    destination_kind: str = DEST_GDRIVE
    r2_account_id: str | None = None
    r2_bucket: str | None = None
    r2_access_key_id: str | None = field(default=None, repr=False)
    r2_secret_access_key: str | None = field(default=None, repr=False)
    r2_prefix: str | None = None
    rclone_remote: str | None = None
    host_label: str = field(default_factory=socket.gethostname)
    work_dir: Path = Path("/var/tmp/styx")
    source_mode: str = SOURCE_BACKEND
    sqlite_path: Path | None = None
    http_endpoint: str | None = None
    http_token: str | None = field(default=None, repr=False)
    min_records: int = DEFAULT_MIN_RECORDS
    min_bytes: int = DEFAULT_MIN_BYTES
    retain_daily: int = DEFAULT_RETAIN_DAILY
    retain_weekly: int = DEFAULT_RETAIN_WEEKLY
    retain_monthly: int = DEFAULT_RETAIN_MONTHLY
    dry_run: bool = False

    def __post_init__(self) -> None:
        if self.destination_kind not in _DESTINATION_KINDS:
            raise StyxConfigError(
                f"{ENV_DESTINATION} must be one of {_DESTINATION_KINDS}, got {self.destination_kind!r}"
            )
        if self.destination_kind == DEST_GDRIVE and not self.gdrive_folder_id:
            raise StyxConfigError(f"{ENV_GDRIVE_FOLDER_ID} is required")
        if self.destination_kind == DEST_R2:
            missing = [
                name
                for name, value in (
                    (ENV_R2_ACCOUNT_ID, self.r2_account_id),
                    (ENV_R2_BUCKET, self.r2_bucket),
                    (ENV_R2_ACCESS_KEY_ID, self.r2_access_key_id),
                    (ENV_R2_SECRET_ACCESS_KEY, self.r2_secret_access_key),
                )
                if not value
            ]
            if missing:
                raise StyxConfigError(f"destination {DEST_R2!r} requires {', '.join(missing)}")
        if self.destination_kind == DEST_RCLONE and not self.rclone_remote:
            raise StyxConfigError(
                f"destination {DEST_RCLONE!r} requires {ENV_RCLONE_REMOTE} "
                "(e.g. 'gdrive-personal:mnemos-backups', a remote already set up "
                "via 'rclone config')"
            )
        if not self.age_recipient:
            raise StyxConfigError(f"{ENV_AGE_RECIPIENT} is required")
        if self.age_recipient.startswith(_AGE_IDENTITY_PREFIX):
            raise StyxConfigError(
                f"{ENV_AGE_RECIPIENT} was given an age IDENTITY (private key), not a "
                "recipient. The private half must never be present on a fleet host — "
                "that is the whole point of encrypting asymmetrically. Supply the "
                f"public {_AGE_RECIPIENT_PREFIX}... recipient instead."
            )
        if not self.age_recipient.startswith(_AGE_RECIPIENT_PREFIX):
            raise StyxConfigError(
                f"{ENV_AGE_RECIPIENT} must be an age recipient starting "
                f"{_AGE_RECIPIENT_PREFIX!r}, got {self.age_recipient[:12]!r}..."
            )
        if self.source_mode not in _SOURCE_MODES:
            raise StyxConfigError(f"{ENV_SOURCE_MODE} must be one of {_SOURCE_MODES}, got {self.source_mode!r}")
        if self.source_mode == SOURCE_BACKEND and self.sqlite_path is None:
            raise StyxConfigError(f"source mode {SOURCE_BACKEND!r} needs {ENV_SQLITE_PATH} (the local store to export)")
        if self.source_mode == SOURCE_HTTP and not self.http_endpoint:
            raise StyxConfigError(f"source mode {SOURCE_HTTP!r} needs {ENV_HTTP_ENDPOINT}")
        if self.min_records < 1:
            raise StyxConfigError(f"{ENV_MIN_RECORDS} must be at least 1 — a zero-record bundle is never a backup")
        if not self.host_label.strip():
            raise StyxConfigError(f"{ENV_HOST_LABEL} must not be blank")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> StyxConfig:
        """Build a config from the process environment (or a supplied mapping)."""
        src = dict(os.environ if env is None else env)
        sqlite_raw = src.get(ENV_SQLITE_PATH)
        return cls(
            gdrive_folder_id=src.get(ENV_GDRIVE_FOLDER_ID, ""),
            age_recipient=src.get(ENV_AGE_RECIPIENT, ""),
            gdrive_credentials=src.get(ENV_GDRIVE_CREDENTIALS),
            destination_kind=src.get(ENV_DESTINATION, DEST_GDRIVE),
            r2_account_id=src.get(ENV_R2_ACCOUNT_ID),
            r2_bucket=src.get(ENV_R2_BUCKET),
            r2_access_key_id=src.get(ENV_R2_ACCESS_KEY_ID),
            r2_secret_access_key=src.get(ENV_R2_SECRET_ACCESS_KEY),
            r2_prefix=src.get(ENV_R2_PREFIX),
            rclone_remote=src.get(ENV_RCLONE_REMOTE),
            host_label=src.get(ENV_HOST_LABEL) or socket.gethostname(),
            work_dir=Path(src.get(ENV_WORK_DIR, "/var/tmp/styx")),
            source_mode=src.get(ENV_SOURCE_MODE, SOURCE_BACKEND),
            sqlite_path=Path(sqlite_raw) if sqlite_raw else None,
            http_endpoint=src.get(ENV_HTTP_ENDPOINT),
            http_token=src.get(ENV_HTTP_TOKEN),
            min_records=_int_env(src, ENV_MIN_RECORDS, DEFAULT_MIN_RECORDS),
            min_bytes=_int_env(src, ENV_MIN_BYTES, DEFAULT_MIN_BYTES),
            retain_daily=_int_env(src, ENV_RETAIN_DAILY, DEFAULT_RETAIN_DAILY),
            retain_weekly=_int_env(src, ENV_RETAIN_WEEKLY, DEFAULT_RETAIN_WEEKLY),
            retain_monthly=_int_env(src, ENV_RETAIN_MONTHLY, DEFAULT_RETAIN_MONTHLY),
            dry_run=_bool_env(src, ENV_DRY_RUN),
        )
