"""Producing the bundle STYX backs up.

Two source modes, both strictly local to the host running STYX — neither
reaches across the network to another instance's data:

``backend``
    Opens the host's own store and exports in-process through
    :func:`mnemos.portability.charon.export_bundle_from_backend` with
    ``include_vault=True, redact_vault=False``. This is the complete backup,
    and it is the mode the ``include_vault`` capability exists for.

``http``
    Streams the host's OWN service export endpoint on loopback. This exists
    because the pooled backends (Oracle, Postgres, Db2, MySQL/MariaDB) build
    their connection pools inside the service's FastAPI lifespan; faithfully
    reconstructing that pool configuration in a separate cron-like process
    is fragile and would drift. Talking to the local service instead reuses
    the exact code path the instance already runs, and stays per-host: no
    cross-host credentials, no cross-host reachability.

The endpoint is required to be loopback unless the operator explicitly opts
out, so a misconfiguration cannot quietly turn per-host STYX into the
centralised puller the design rejected.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import tarfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

logger = logging.getLogger(__name__)

from mnemos.tools.styx.config import SOURCE_BACKEND, SOURCE_HTTP, StyxConfig
from mnemos.tools.styx.errors import StyxConfigError, StyxError
from mnemos.tools.export_stream import completion_count_matches

_STREAM_CHUNK = 1024 * 256
_HTTP_EXPORT_PAGE_LIMIT = 1000
_LOOPBACK_HOSTS = {"localhost", "ip6-localhost"}


@dataclass(frozen=True)
class BundleResult:
    """A produced, not-yet-archived backup payload."""

    path: Path
    record_count: int
    kind: str
    vault_included: bool


def _is_loopback(endpoint: str) -> bool:
    host = urllib.parse.urlparse(endpoint).hostname or ""
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def _produce_from_backend(config: StyxConfig, dest: Path) -> BundleResult:
    from mnemos.persistence.sqlite import (
        SqliteBackend,  # noqa: PLC0415 - optional backend
    )
    from mnemos.portability import charon  # noqa: PLC0415 - core dependency

    sqlite_path = config.sqlite_path
    if sqlite_path is None or not sqlite_path.exists():
        raise StyxConfigError(f"no MNEMOS store to export at {sqlite_path}")

    backend = SqliteBackend(sqlite_path, SimpleNamespace())
    await backend.open()
    try:
        manifest = await charon.export_bundle_from_backend(
            backend,
            dest,
            include_sidecars=True,
            # The two flags that make this a real backup rather than a
            # partial one. redact_vault=False is deliberate and is why the
            # artifact must never leave this host unencrypted.
            include_vault=True,
            redact_vault=False,
        )
    finally:
        await backend.close()

    if not manifest.get("vault_included"):
        raise StyxError(
            "the MIF export reported vault_included=False. This build of mnemos-core "
            "predates the include_vault capability, so the bundle is missing the "
            "vault namespace and is not a complete backup. Refusing to upload it."
        )
    return BundleResult(
        path=dest,
        record_count=int(manifest.get("count", 0)),
        kind="mif-bundle",
        vault_included=True,
    )


def _produce_from_http(config: StyxConfig, dest: Path, *, allow_remote: bool = False) -> BundleResult:
    endpoint = config.http_endpoint or ""
    if not allow_remote and not _is_loopback(endpoint):
        raise StyxConfigError(
            f"STYX http source must point at this host's own service, got {endpoint!r}. "
            "STYX is per-host by design; pulling another instance over the network "
            "reintroduces the centralised-puller failure mode it avoids."
        )

    parts = urllib.parse.urlsplit(endpoint)
    query = dict(urllib.parse.parse_qsl(parts.query))
    # include_secrets is the service-side equivalent of include_vault: the
    # documented root-only backup escape hatch. Without it the export silently
    # omits the vault namespace.
    # ``limit`` is a page size, not a corpus cap, on the streaming route.  The
    # server advances a (created,id) keyset cursor inside one repeatable-read
    # snapshot until it emits the completion marker.  Keep it explicit so this
    # client never inherits a changed route default. Page framing is
    # intentional: live Oracle validation completed about seven times faster
    # than one-line-per-record framing, while the validator below counts the
    # records inside every page instead of mistaking frames for records.
    query.update(
        {
            "stream": "true",
            "stream_records": "false",
            "limit": str(_HTTP_EXPORT_PAGE_LIMIT),
            "offset": "0",
            "include_sidecars": "true",
            "include_secrets": "true",
            "mpf_version": "0.2",
        }
    )
    url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))

    request = urllib.request.Request(  # noqa: S310 - loopback/operator-configured endpoint
        url, headers={"Accept": "application/x-ndjson"}
    )
    if config.http_token:
        request.add_header("Authorization", f"Bearer {config.http_token}")

    dest.mkdir(parents=True, exist_ok=True)
    payload = dest / "export.ndjson"
    try:
        with (
            urllib.request.urlopen(request, timeout=900) as response,  # noqa: S310
            payload.open("wb") as handle,
        ):
            content_type = response.headers.get("Content-Type", "").lower()
            if "application/x-ndjson" not in content_type:
                raise StyxError(f"streaming export returned unexpected content type {content_type or '<missing>'!r}")
            while True:
                chunk = response.read(_STREAM_CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
        record_count = _validate_http_export(payload)
        # The wire artifact is an NDJSON sequence of page envelopes, not the
        # JSONL record format accepted by ``memory_import``. Materialize one
        # validated MPF envelope so an encrypted STYX artifact is directly
        # restorable after extraction instead of merely being a transport
        # capture that needs an undocumented conversion step.
        from mnemos.tools.export_stream import write_export

        mpf_payload = dest / "export.mpf.json"
        with payload.open("r", encoding="utf-8") as handle:
            written = write_export((json.loads(line) for line in handle if line.strip()), mpf_payload)
        if written != record_count:
            raise StyxError(
                f"materialized MPF record count does not match validated stream ({written} != {record_count})"
            )
        payload.unlink()
    except StyxError:
        payload.unlink(missing_ok=True)
        (dest / "export.mpf.json").unlink(missing_ok=True)
        raise
    except Exception as exc:
        payload.unlink(missing_ok=True)
        (dest / "export.mpf.json").unlink(missing_ok=True)
        raise StyxError(f"streaming export from {parts.path} failed: {exc}") from exc

    (dest / "styx-source.json").write_text(
        json.dumps(
            {
                "source": "http",
                "endpoint": parts.path,
                "records": record_count,
                "page_size": _HTTP_EXPORT_PAGE_LIMIT,
                "snapshot": "repeatable-read",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return BundleResult(path=dest, record_count=record_count, kind="mpf-envelope", vault_included=True)


def _validate_http_export(payload: Path) -> int:
    """Return the protocol record count, rejecting any incomplete stream.

    NDJSON lines are protocol frames, not records: page framing can put up to
    ``limit`` records on one line, while per-record framing puts one on each.
    A clean transport EOF is not proof that the application completed.  The
    terminal marker is the snapshot's commit record and its declared count
    must equal the records that actually arrived.

    Duplicate-identity policy (the keyset-seam defect, where the server
    re-emits the last row(s) of a page as the first row(s) of the next):

    * a duplicate identity whose payload is byte-identical to the first
      occurrence is TOLERATED: logged as a WARNING naming the duplicated
      ids, and the returned count is the UNIQUE identity count, which is
      exactly what ``write_export`` materializes (it deduplicates by
      identity), so the two counts compare the same set;
    * a duplicate identity with a DIFFERENT payload still fails hard —
      that is corruption, not a pagination seam;
    * the completion marker must equal either the unique count (seam-guarded
      servers) or the received wire count (deployed servers count every
      frame they send, duplicates included). See
      :func:`mnemos.tools.export_stream.completion_count_matches`.

    The 2026-11-01..03 PYTHIA failures (``28415 != 28418``) were this
    function returning the WIRE count while ``write_export`` returned the
    unique count: two counters over different sets, not a snapshot race.
    """
    record_count = 0
    completed = False
    unique_count = 0
    payloads_by_identity: dict[str, str] = {}
    duplicates: list[str] = []
    try:
        with payload.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                if completed:
                    raise StyxError("streaming export sent data after its completion marker")
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise StyxError(f"streaming export contains invalid NDJSON at line {line_number}") from exc
                if not isinstance(frame, dict):
                    raise StyxError(f"streaming export frame {line_number} is not an object")
                if frame.get("export_complete") is True:
                    declared = frame.get("record_count")
                    if isinstance(declared, bool) or not isinstance(declared, int) or declared < 0:
                        raise StyxError("streaming export completion marker has an invalid record_count")
                    if frame.get("records") not in (None, []):
                        raise StyxError("streaming export completion marker also contains records")
                    if not completion_count_matches(declared, unique=unique_count, wire=record_count):
                        raise StyxError(
                            "streaming export completion count does not match unique "
                            f"record identities or received records ({declared} vs "
                            f"{unique_count} unique / {record_count} received)"
                        )
                    if duplicates:
                        logger.warning(
                            "streaming export contained %d byte-identical duplicate "
                            "record(s) at page boundaries; counted unique identities. "
                            "duplicated ids: %s",
                            len(duplicates),
                            ", ".join(duplicates[:20]) + (" …" if len(duplicates) > 20 else ""),
                        )
                    completed = True
                    continue
                records = frame.get("records")
                if not isinstance(records, list):
                    raise StyxError(f"streaming export frame {line_number} has no records array")
                record_count += len(records)
                for entry in records:
                    if not isinstance(entry, dict):
                        raise StyxError(f"streaming export frame {line_number} has a non-object record")
                    identity = entry.get("id")
                    if not isinstance(identity, str) or not identity:
                        raise StyxError(f"streaming export frame {line_number} has a record without an identity")
                    serialized = json.dumps(entry, sort_keys=True, ensure_ascii=False)
                    prior = payloads_by_identity.get(identity)
                    if prior is None:
                        payloads_by_identity[identity] = serialized
                        unique_count += 1
                    elif prior != serialized:
                        raise StyxError(
                            f"streaming export re-emitted record {identity!r} with a "
                            "DIFFERENT payload (first at line unknown, again at line "
                            f"{line_number}); refusing to back up a divergent stream"
                        )
                    elif identity not in duplicates:
                        duplicates.append(identity)
    except UnicodeDecodeError as exc:
        raise StyxError("streaming export is not valid UTF-8 NDJSON") from exc
    if not completed:
        raise StyxError("streaming export ended before its completion marker")
    return unique_count


async def produce_bundle(config: StyxConfig, dest: Path, *, allow_remote_http: bool = False) -> BundleResult:
    """Produce the backup payload for this host under ``dest``."""
    if config.source_mode == SOURCE_BACKEND:
        return await _produce_from_backend(config, dest)
    if config.source_mode == SOURCE_HTTP:
        return _produce_from_http(config, dest, allow_remote=allow_remote_http)
    raise StyxConfigError(f"unknown source mode {config.source_mode!r}")


def archive_bundle(bundle_dir: Path, archive_path: Path) -> Path:
    """Tar+gzip ``bundle_dir`` into ``archive_path``.

    Paths are stored relative to the bundle root so an operator can unpack
    the restored tree anywhere, and entries are sorted so two exports of an
    unchanged corpus differ only by their gzip timestamp rather than by
    filesystem iteration order.
    """
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    members = sorted(bundle_dir.rglob("*"), key=lambda p: str(p.relative_to(bundle_dir)))
    with tarfile.open(archive_path, "w:gz") as tar:
        for member in members:
            tar.add(member, arcname=str(member.relative_to(bundle_dir)), recursive=False)
    if not archive_path.exists() or archive_path.stat().st_size == 0:
        raise StyxError(f"archiving produced no output at {archive_path}")
    return archive_path
