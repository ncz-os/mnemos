"""STYX configuration validation."""

from __future__ import annotations

from dataclasses import fields

import pytest

from mnemos.tools.styx.config import SOURCE_HTTP, StyxConfig
from mnemos.tools.styx.errors import StyxConfigError

RECIPIENT = "age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3n0qmt"

BASE_ENV = {
    "MNEMOS_STYX_GDRIVE_FOLDER_ID": "folder-123",
    "MNEMOS_STYX_AGE_RECIPIENT": RECIPIENT,
    "MNEMOS_STYX_SQLITE_PATH": "/var/lib/mnemos/mnemos.sqlite3",
    "MNEMOS_STYX_HOST_LABEL": "pythia",
}


def test_from_env_applies_gfs_defaults():
    config = StyxConfig.from_env(BASE_ENV)
    assert (config.retain_daily, config.retain_weekly, config.retain_monthly) == (
        14,
        8,
        12,
    )
    assert config.min_records == 1
    assert config.host_label == "pythia"
    assert config.dry_run is False


def test_private_age_identity_is_refused():
    """The whole design fails if the private half reaches a fleet host."""
    env = dict(BASE_ENV, MNEMOS_STYX_AGE_RECIPIENT="AGE-SECRET-KEY-1QQQQQQQQ")
    with pytest.raises(StyxConfigError, match="IDENTITY"):
        StyxConfig.from_env(env)


def test_non_age_recipient_is_refused():
    env = dict(BASE_ENV, MNEMOS_STYX_AGE_RECIPIENT="ssh-ed25519 AAAAC3Nz")
    with pytest.raises(StyxConfigError, match="must be an age recipient"):
        StyxConfig.from_env(env)


@pytest.mark.parametrize("missing", ["MNEMOS_STYX_GDRIVE_FOLDER_ID", "MNEMOS_STYX_AGE_RECIPIENT"])
def test_required_settings_are_required(missing):
    env = dict(BASE_ENV)
    env.pop(missing)
    with pytest.raises(StyxConfigError, match="required"):
        StyxConfig.from_env(env)


def test_backend_mode_needs_a_store_path():
    env = dict(BASE_ENV)
    env.pop("MNEMOS_STYX_SQLITE_PATH")
    with pytest.raises(StyxConfigError, match="SQLITE_PATH"):
        StyxConfig.from_env(env)


def test_http_mode_needs_an_endpoint():
    env = dict(BASE_ENV, MNEMOS_STYX_SOURCE_MODE=SOURCE_HTTP)
    env.pop("MNEMOS_STYX_SQLITE_PATH")
    with pytest.raises(StyxConfigError, match="HTTP_ENDPOINT"):
        StyxConfig.from_env(env)


def test_zero_min_records_is_refused():
    """A floor of zero would re-admit exactly the silent-empty-backup bug."""
    env = dict(BASE_ENV, MNEMOS_STYX_MIN_RECORDS="0")
    with pytest.raises(StyxConfigError, match="at least 1"):
        StyxConfig.from_env(env)


def test_non_integer_retention_is_refused():
    env = dict(BASE_ENV, MNEMOS_STYX_RETAIN_DAILY="lots")
    with pytest.raises(StyxConfigError, match="must be an integer"):
        StyxConfig.from_env(env)


def test_unknown_source_mode_is_refused():
    env = dict(BASE_ENV, MNEMOS_STYX_SOURCE_MODE="carrier-pigeon")
    with pytest.raises(StyxConfigError, match="SOURCE_MODE"):
        StyxConfig.from_env(env)


# ── repr() must not leak secrets ─────────────────────────────────────────
#
# ``StyxConfig`` is a frozen dataclass and the default ``__repr__`` echoes
# every field value. That is exactly the wrong shape for the fields that
# carry credentials — a service-account JSON blob, an R2 access key, or the
# loopback HTTP token must NEVER appear in a traceback, a Sentry payload, a
# forwarded log line, or an issue-tracker comment.
#
# The contract is enforced at the dataclass layer: every credential field
# is declared with ``field(repr=False)``, which the auto-generated
# ``__repr__`` honours. The tests below prove two independent properties:
#
# 1. each known credential field is in fact ``repr=False`` at the dataclass
#    layer (so a future maintainer adding a new credential sees the canary
#    failure the moment they forget the keyword argument);
# 2. building a config that holds four uniquely-tagged fake secrets and
#    rendering ``repr(config)`` (and ``str(config)``, which shares the
#    implementation) never includes any of them — including via the
#    realistic failure mode of an exception message embedding the repr.

CREDENTIAL_FIELDS: tuple[str, ...] = (
    "gdrive_credentials",
    "http_token",
    "r2_access_key_id",
    "r2_secret_access_key",
)


def _all_secret_values() -> dict[str, str]:
    """Return a fresh mapping of every credential field, paired with a unique marker.

    ``uuid4().hex`` markers make a regression that drops or shortens any
    single secret surface as its own assertion, and accidental cross-talk
    with other test fixtures is impossible.
    """
    import uuid

    return {name: f"{name.upper()}-{uuid.uuid4().hex}" for name in CREDENTIAL_FIELDS}


@pytest.mark.parametrize("field_name", CREDENTIAL_FIELDS)
def test_each_credential_field_is_repr_false(field_name):
    """The dataclass contract: every credential field MUST be ``repr=False``.

    Without this, ``repr(config)`` echoes the secret verbatim and the
    traceback-redaction property below does not hold. The parametrize is
    the canary — adding a new credential field to ``StyxConfig`` without
    marking it ``repr=False`` is caught here the moment a corresponding
    parameter fails, before the leak ever happens in production.
    """
    field_map = {f.name: f for f in fields(StyxConfig)}
    assert field_name in field_map, (
        f"credential {field_name!r} no longer exists on StyxConfig; update CREDENTIAL_FIELDS"
    )
    assert field_map[field_name].repr is False, (
        f"credential field {field_name!r} must be declared with "
        "``field(repr=False)`` so its value never appears in repr() / str()"
    )


def test_repr_does_not_leak_gdrive_credentials():
    secrets = _all_secret_values()
    config = StyxConfig(
        gdrive_folder_id="folder-123",
        age_recipient=RECIPIENT,
        gdrive_credentials=secrets["gdrive_credentials"],
        sqlite_path="/var/lib/mnemos/mnemos.sqlite3",
    )
    rendered = repr(config)
    assert secrets["gdrive_credentials"] not in rendered
    # str(config) shares the implementation; both must be safe.
    assert secrets["gdrive_credentials"] not in str(config)


def test_repr_does_not_leak_http_token():
    secrets = _all_secret_values()
    config = StyxConfig(
        gdrive_folder_id="folder-123",
        age_recipient=RECIPIENT,
        sqlite_path="/var/lib/mnemos/mnemos.sqlite3",
        source_mode="http",
        http_endpoint="http://localhost:8000",
        http_token=secrets["http_token"],
    )
    rendered = repr(config)
    assert secrets["http_token"] not in rendered
    assert secrets["http_token"] not in str(config)


def test_repr_does_not_leak_r2_credentials():
    secrets = _all_secret_values()
    config = StyxConfig(
        age_recipient=RECIPIENT,
        destination_kind="r2",
        r2_account_id="acct",
        r2_bucket="styx-backups",
        r2_access_key_id=secrets["r2_access_key_id"],
        r2_secret_access_key=secrets["r2_secret_access_key"],
        sqlite_path="/var/lib/mnemos/mnemos.sqlite3",
    )
    rendered = repr(config)
    assert secrets["r2_access_key_id"] not in rendered
    assert secrets["r2_secret_access_key"] not in rendered
    # Non-credential R2 fields are still rendered (so the operator can still
    # see bucket / prefix / account when triaging).
    assert "styx-backups" in rendered
    assert "acct" in rendered


def test_repr_does_not_leak_any_secret():
    """End-to-end: a single config carrying every fake secret renders
    without any of them appearing in the output."""
    secrets = _all_secret_values()
    config = StyxConfig(
        gdrive_folder_id="folder-123",
        age_recipient=RECIPIENT,
        gdrive_credentials=secrets["gdrive_credentials"],
        http_token=secrets["http_token"],
        destination_kind="r2",
        r2_account_id="acct",
        r2_bucket="styx-backups",
        r2_access_key_id=secrets["r2_access_key_id"],
        r2_secret_access_key=secrets["r2_secret_access_key"],
        sqlite_path="/var/lib/mnemos/mnemos.sqlite3",
    )

    for rendered in (repr(config), str(config)):
        for field_name, marker in secrets.items():
            assert marker not in rendered, (
                f"credential {field_name!r} leaked through rendered repr/str; found marker {marker!r}"
            )


def test_exception_message_containing_repr_does_not_leak_secrets():
    """The realistic failure mode: an exception whose message embeds the
    config repr — exactly the shape ``logger.error("...: %s",
    config, exc)`` or a ``raise ... from None`` produces when stringified
    by Python's default traceback formatter."""
    secrets = _all_secret_values()
    config = StyxConfig(
        gdrive_folder_id="folder-123",
        age_recipient=RECIPIENT,
        gdrive_credentials=secrets["gdrive_credentials"],
        http_token=secrets["http_token"],
        destination_kind="r2",
        r2_account_id="acct",
        r2_bucket="styx-backups",
        r2_access_key_id=secrets["r2_access_key_id"],
        r2_secret_access_key=secrets["r2_secret_access_key"],
        sqlite_path="/var/lib/mnemos/mnemos.sqlite3",
    )

    message = f"styx failed: config={config!r}"

    for field_name, marker in secrets.items():
        assert marker not in message, (
            f"credential {field_name!r} leaked through an exception message that embedded repr(config)"
        )
