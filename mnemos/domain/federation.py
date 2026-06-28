"""Federation sync engine — pull memories from remote MNEMOS peers.

Pull model: each peer is a remote instance; we periodically fetch their
`/v1/federation/feed` endpoint with a Bearer token they issued us. Memories
are stored locally with id = `fed:{peer_name}:{remote_id}` and
`federation_source = peer_name`, dedupable on re-pull via the id + updated
timestamp.

Peers are configured via admin endpoints (api/handlers/federation.py). A
lifespan-owned worker iterates enabled peers on their individual sync
intervals.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, NamedTuple, Optional, Tuple, Union

import httpx

from mnemos.core import eligibility as _eligibility
from mnemos.core.persisted_text_classification import classify_persisted_text_fields
from mnemos.core.safe_http import make_safe_client
from mnemos.persistence.base import AuditPersistence, FederationPersistence, FederationRepository, Transaction

FederationBackend = Union[FederationPersistence, AuditPersistence]

logger = logging.getLogger(__name__)


def _federation_allow_private() -> bool:
    """Lazy-read FEDERATION_ALLOW_PRIVATE so module import never loads config."""
    from mnemos.core.config import get_settings

    return get_settings().federation.allow_private

# Keep this legacy module import-compatible while allowing additive
# submodules under mnemos/domain/federation/.
__path__ = [os.path.join(os.path.dirname(__file__), "federation")]

FEDERATION_HTTP_TIMEOUT = 30.0
FEDERATION_BATCH_LIMIT = 100
FEDERATION_ID_PREFIX = "fed:"
FEDERATION_CURSOR_LOWER_ID = ""
# Per-field size caps for incoming peer payloads. Hostile peers can otherwise
# fill disk by pushing 50MB blobs; these caps bound a single memory to ~1.5MB.
FEDERATION_MAX_CONTENT = 1_000_000  # 1 MB per content body
FEDERATION_MAX_METADATA = 64 * 1024  # 64 KB metadata json
FEDERATION_MAX_NAME = 256  # category/subcategory/namespace length


def eligible_for_federation(alias: str = "m") -> str:
    return _eligibility.eligible_for_federation(alias)


class FederationFeedCursor(NamedTuple):
    updated: datetime
    memory_id: str


def _cursor_timestamp_for_wire(updated: datetime) -> str:
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    else:
        updated = updated.astimezone(timezone.utc)
    return updated.isoformat().replace("+00:00", "Z")


def _cursor_timestamp_for_db(updated: datetime) -> datetime:
    if updated.tzinfo is None:
        return updated.replace(tzinfo=timezone.utc)
    return updated.astimezone(timezone.utc)


def _parse_cursor_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _encode_feed_cursor(updated: datetime, memory_id: str) -> str:
    payload = {
        "updated": _cursor_timestamp_for_wire(updated),
        "id": memory_id,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_feed_cursor(raw: str) -> FederationFeedCursor:
    """Decode a compound federation cursor."""
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("cursor payload must be an object")
        updated_raw = payload.get("updated")
        if not isinstance(updated_raw, str):
            raise ValueError("cursor payload missing updated")
        updated = _parse_cursor_timestamp(updated_raw)
        memory_id = payload.get("id")
        if not isinstance(memory_id, str):
            raise ValueError("cursor payload missing id")
        return FederationFeedCursor(updated=updated, memory_id=memory_id)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("invalid federation cursor")


def _cap(value, limit: int):
    """Truncate strings above `limit`. Pass-through for None/non-string."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit]
    return value


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ── Pull + store ─────────────────────────────────────────────────────────────


class FederationSchemaError(Exception):
    """Schema-compat preflight aborted the sync.

    Subclasses carry the kind of failure so the API surface can map
    to the right HTTP status (vs collapsing every sync_peer ValueError
    to 404 — Codex review-round-3 finding #2).
    """


class FederationSchemaIncompatible(FederationSchemaError):
    """Confirmed mismatch: peer responded but schema_signature or
    migrations_fingerprint differs from local. → HTTP 409."""


class FederationSchemaUnverifiable(FederationSchemaError):
    """Peer responded with a definitive 4xx (no /schema endpoint, bad
    auth, etc.) — peer is durably non-v3.4-compatible. → HTTP 409."""


class FederationSchemaTransient(FederationSchemaError):
    """Could not reach peer's /schema endpoint (network error, timeout,
    5xx). Sync should NOT consume the full sync_interval_secs — the
    next worker tick can retry. → HTTP 503."""


async def _check_peer_schema(
    base_url: str,
    auth_token: str,
    name: str,
) -> Dict[str, Any]:
    """GET peer's /v1/federation/schema.

    Returns a dict — never raises. Shape:
        {"ok": True,  "mnemos_version": str, "schema_signature": str,
         "migrations_fingerprint": str|None}
        {"ok": False, "transient": bool, "reason": str}

    `ok=False` + `transient=False` means the peer responded but is
    durably incompatible (4xx, missing fields). Strict mode MUST
    treat as a hard fail (Codex review-round-1 finding #1).

    `ok=False` + `transient=True` means we could not reach the peer
    (network error, timeout, 5xx). Strict mode should retry on the
    next worker tick rather than burning the full sync_interval_secs
    (Codex review-round-3 finding #1).
    """
    url = f"{base_url.rstrip('/')}/v1/federation/schema"
    # F1 (adversarial review 2026-06-28): re-validate the peer URL against the
    # SSRF blocklist at fetch time and pin DNS so a DNS-rebinding TOCTOU
    # between peer registration and this sync cannot redirect the
    # authenticated pull (carrying the peer bearer token) to an
    # internal/metadata endpoint. A validation failure is durable: the
    # peer URL is misconfigured or compromised and retrying will not help.
    try:
        client, _ = await make_safe_client(
            url, timeout=10.0, allow_private=_federation_allow_private(),
        )
    except Exception as e:
        logger.warning("federation: peer %s URL rejected (SSRF/DNS): %s", name, e)
        return {"ok": False, "transient": False, "reason": f"url-rejected: {type(e).__name__}: {e}"}
    try:
        async with client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {auth_token}"})
            if resp.status_code >= 500:
                # 5xx — transient infra failure on the peer.
                logger.warning(
                    "federation: peer %s /schema returned %d (transient)",
                    name,
                    resp.status_code,
                )
                return {
                    "ok": False,
                    "transient": True,
                    "reason": f"http {resp.status_code}",
                }
            if resp.status_code != 200:
                # 4xx — durable: peer doesn't speak the protocol or
                # rejected the auth. Pre-v3.4 peers land here too.
                logger.info(
                    "federation: peer %s /schema returned %d — peer may pre-date v3.4 federation_compat",
                    name,
                    resp.status_code,
                )
                return {
                    "ok": False,
                    "transient": False,
                    "reason": f"http {resp.status_code}",
                }
            try:
                data = resp.json()
            except Exception as parse_err:
                # 200 but unparseable JSON — durable shape problem.
                return {
                    "ok": False,
                    "transient": False,
                    "reason": f"unparseable schema response: {parse_err}",
                }
            mnemos_version = data.get("mnemos_version")
            schema_signature = data.get("schema_signature")
            if not mnemos_version or not schema_signature:
                return {
                    "ok": False,
                    "transient": False,
                    "reason": "missing mnemos_version or schema_signature",
                }
            return {
                "ok": True,
                "mnemos_version": mnemos_version,
                "schema_signature": schema_signature,
                # Optional — older v3.4 builds may not return it; treat
                # as None and skip fingerprint comparison if absent.
                "migrations_fingerprint": data.get("migrations_fingerprint"),
            }
    # Codex review-round-4 finding #2 — expand transient envelope to
    # include the rest of httpx's retryable transport errors. Keep
    # local/config failures (InvalidURL, UnsupportedProtocol,
    # LocalProtocolError) durable so a misconfigured peer doesn't
    # spin forever on transient retries.
    except (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.NetworkError,
        httpx.RemoteProtocolError,
        httpx.ProxyError,
    ) as e:
        logger.warning(
            "federation: peer %s /schema fetch failed (transient): %s",
            name,
            e,
        )
        return {
            "ok": False,
            "transient": True,
            "reason": f"{type(e).__name__}: {e}",
        }
    except (httpx.InvalidURL, httpx.UnsupportedProtocol, httpx.LocalProtocolError) as e:
        logger.warning(
            "federation: peer %s /schema config error: %s",
            name,
            e,
        )
        return {
            "ok": False,
            "transient": False,
            "reason": f"{type(e).__name__}: {e}",
        }
    except Exception as e:
        # Unrecognized — record but treat as durable so transient
        # backoff doesn't loop forever on a programming error.
        logger.warning(
            "federation: peer %s /schema fetch failed: %s",
            name,
            e,
        )
        return {
            "ok": False,
            "transient": False,
            "reason": f"{type(e).__name__}: {e}",
        }


_MIGRATIONS_FINGERPRINT_CACHE: Optional[str] = None


def _local_migrations_fingerprint() -> str:
    """Deterministic SHA256-prefix over (filename, content) of every
    migration in the deployed source tree.

    Codex review-round-1 finding #3 + round-7 finding #1: hashing
    filenames alone misses content drift — a downstream fork could
    edit the SQL inside an existing filename without changing the
    name. Hashing filename + file content catches that case. We
    cache the result at module load (migrations are immutable in a
    deployed container; recomputing on every /schema GET would burn
    disk I/O for no signal change).

    Limitations (deliberately deferred to V3_5_CHARTER):
      - We hash *deployed* migration files, not migrations *applied*
        to the running database. A migration that failed at apply-
        time still contributes to the fingerprint. Closing this gap
        means querying information_schema (or a migration ledger
        table) at /schema-serving time, which is more expensive and
        scoped for the "core fields + extensions" contract work.
      - The hash includes only db/migrations*.sql — handler-level
        contract changes (new endpoints, payload shape changes) are
        not captured here; mnemos_version + schema_signature carry
        that signal.
    """
    global _MIGRATIONS_FINGERPRINT_CACHE
    if _MIGRATIONS_FINGERPRINT_CACHE is not None:
        return _MIGRATIONS_FINGERPRINT_CACHE
    import hashlib
    from pathlib import Path

    db_dir = Path(__file__).resolve().parents[2] / "db"
    if not db_dir.is_dir():
        _MIGRATIONS_FINGERPRINT_CACHE = ""
        return ""
    h = hashlib.sha256()
    for p in sorted(db_dir.glob("migrations*.sql")):
        h.update(p.name.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            # Permission/IO error reading a migration file — record
            # the name + a sentinel so the result still differentiates
            # this deployment from one where the file is readable.
            h.update(b"<unreadable>")
        h.update(b"\0\0")
    _MIGRATIONS_FINGERPRINT_CACHE = h.hexdigest()[:16]
    return _MIGRATIONS_FINGERPRINT_CACHE


async def sync_peer(
    backend: FederationBackend,
    peer_id: str,
) -> Tuple[int, int, int]:
    """Run a full sync against one peer. Returns (pulled, new, updated).

    Pre-flight: query peer's /v1/federation/schema and compare
    schema_signature (major.minor) against ours. If mismatched and
    peer.compat_mode == 'strict', abort the sync with a clear error.
    Operators must explicitly set compat_mode='permissive' on a peer
    to allow cross-version sync.
    """
    repo = backend.federation
    async with backend.transactional() as tx:
        peer = await repo.get_sync_peer(tx, peer_id)
    if not peer:
        raise ValueError(f"peer {peer_id} not found")
    if not peer["enabled"]:
        logger.info("federation: peer %s disabled — skipping", peer["name"])
        return 0, 0, 0
    cursor_before = _coerce_datetime(peer["last_sync_cursor"])

    # Schema-compatibility pre-flight (added in v3.4 federation_compat).
    # See db/migrations_v3_4_federation_compat.sql for column meaning.
    from mnemos._version import __version__ as _local_v

    _local_parts = _local_v.split(".")
    local_signature = f"{_local_parts[0]}.{_local_parts[1]}" if len(_local_parts) >= 2 else _local_v
    local_fingerprint = _local_migrations_fingerprint()
    schema_resp = await _check_peer_schema(
        peer["base_url"],
        peer["auth_token"],
        peer["name"],
    )

    schema_abort_reason: Optional[str] = None
    schema_abort_kind: Optional[str] = None  # 'incompat'|'unverifiable'|'transient'
    peer_version: Optional[str] = None
    if schema_resp["ok"]:
        peer_version = schema_resp["mnemos_version"]
        peer_signature = schema_resp["schema_signature"]
        peer_fingerprint = schema_resp.get("migrations_fingerprint")
        sig_match = peer_signature == local_signature
        if not sig_match:
            schema_abort_reason = (
                f"schema mismatch: peer={peer_signature} ({peer_version}) local={local_signature} ({_local_v})"
            )
            schema_abort_kind = "incompat"
        elif local_fingerprint == "":
            # We can't compute our own fingerprint (e.g. test rig
            # without a db/ directory). Falling back to signature-only
            # is the only option — accept.
            pass
        elif peer_fingerprint is None:
            # Codex review-round-6 finding #1 — peer at same major.minor
            # but doesn't expose migrations_fingerprint. Treat as
            # unverifiable in strict (peer might be a forked v3.4 with
            # extra/missing migrations). Operator can flip to permissive
            # if they trust the peer.
            schema_abort_reason = (
                f"peer {peer_version} matches signature {local_signature} "
                f"but does not expose migrations_fingerprint — cannot "
                f"verify same-minor schema drift"
            )
            schema_abort_kind = "unverifiable"
        elif peer_fingerprint != local_fingerprint:
            schema_abort_reason = (
                f"migrations fingerprint mismatch within {local_signature}: "
                f"peer={peer_fingerprint} local={local_fingerprint} "
                f"(peer={peer_version} local={_local_v})"
            )
            schema_abort_kind = "incompat"
    else:
        # Codex finding #1 (round 1) + #1 (round 3) — distinguish
        # transient (network/timeout/5xx) from durable (4xx, parse).
        # Both fail strict, but transient should NOT burn the full
        # sync_interval_secs — see strict-abort branch below.
        schema_abort_reason = f"schema unverifiable ({schema_resp['reason']})"
        schema_abort_kind = "transient" if schema_resp.get("transient") else "unverifiable"
    if schema_abort_reason is not None and peer["compat_mode"] == "strict":
        # Codex review-round-2 finding #2 — schema-metadata update,
        # sync_log row, and peer last_sync_at advance MUST commit as a
        # single transaction. A crash between split connections would
        # leave the peer with last_schema_check_at fresh but no log
        # row and no last_sync_at advance, putting the worker right
        # back into a tight retry loop on its next 60s scan.
        #
        # Codex review-round-3 finding #1 — transient probe failures
        # (network/timeout/5xx) should NOT burn the full
        # sync_interval_secs. We still record the failure in the log
        # and update peer metadata, but skip the last_sync_at advance
        # so the next 60s worker tick can re-attempt. Durable failures
        # (incompat, 4xx, parse) advance last_sync_at as normal.
        is_transient = schema_abort_kind == "transient"
        async with backend.transactional() as tx:
            await repo.record_schema_abort(
                tx,
                peer_id=peer_id,
                peer_version=peer_version,
                cursor_before=cursor_before,
                error=schema_abort_reason,
                is_transient=is_transient,
            )
        logger.error(
            "federation: peer %s — strict abort (%s): %s",
            peer["name"],
            schema_abort_kind,
            schema_abort_reason,
        )
        msg = (
            f"federation peer {peer['name']}: {schema_abort_reason}. "
            f"Set compat_mode='permissive' on the peer to allow "
            f"cross-version sync."
        )
        # Codex review-round-3 finding #2 — typed exceptions so the
        # API surface can map to the right HTTP status (was: every
        # ValueError → 404 "peer not found").
        if schema_abort_kind == "incompat":
            raise FederationSchemaIncompatible(msg)
        if schema_abort_kind == "transient":
            raise FederationSchemaTransient(msg)
        raise FederationSchemaUnverifiable(msg)

    # Non-strict-abort paths: still record what we learned about the
    # peer so operators have visibility into "last seen version X".
    async with backend.transactional() as tx:
        await repo.update_peer_schema_check(tx, peer_id, peer_version)

    if schema_abort_reason is not None:
        # compat_mode == 'permissive' falls through to here.
        logger.warning(
            "federation: peer %s — permissive, proceeding despite: %s",
            peer["name"],
            schema_abort_reason,
        )
    else:
        logger.debug(
            "federation: peer %s schema-aligned at %s",
            peer["name"],
            local_signature,
        )

    async with backend.transactional() as tx:
        log_id = await repo.create_sync_log(tx, peer_id, cursor_before)

    total_pulled = 0
    total_new = 0
    total_updated = 0
    cursor_request: Optional[datetime | FederationFeedCursor] = cursor_before
    cursor_persisted = cursor_before
    err: Optional[str] = None

    # v6.1 F-1: per-peer copy_embeddings flag (added by migration 0028).
    # Pass via _pull_batch query param so source emits embedding bytes.
    peer_copy_embeddings = False
    try:
        ce = peer.get("copy_embeddings") if hasattr(peer, "get") else peer["copy_embeddings"]
        peer_copy_embeddings = bool(ce) if ce is not None else False
    except (KeyError, IndexError, TypeError):
        peer_copy_embeddings = False

    try:
        while True:
            batch, next_cursor, has_more = await _pull_batch(
                peer["base_url"],
                peer["auth_token"],
                cursor_request,
                peer["namespace_filter"],
                peer["category_filter"],
                copy_embeddings=peer_copy_embeddings,
            )
            if not batch:
                break
            async with backend.transactional() as tx:
                new_n, upd_n = await _store_memories(repo, tx, peer["name"], batch, backend=backend)
            total_pulled += len(batch)
            total_new += new_n
            total_updated += upd_n
            if next_cursor is not None:
                cursor_request = next_cursor
                cursor_persisted = next_cursor.updated
            if not has_more:
                break
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        logger.exception("federation: pull from %s failed", peer["name"])

    async with backend.transactional() as tx:
        await repo.finish_sync_log(
            tx,
            log_id=log_id,
            memories_pulled=total_pulled,
            memories_new=total_new,
            memories_updated=total_updated,
            error=err,
            cursor_after=cursor_persisted,
        )
        if err:
            await repo.record_sync_error(tx, peer_id, err)
        else:
            await repo.record_sync_success(tx, peer_id, cursor_persisted, total_pulled)

    logger.info(
        "federation: peer=%s pulled=%d new=%d updated=%d cursor=%s",
        peer["name"],
        total_pulled,
        total_new,
        total_updated,
        cursor_persisted,
    )
    return total_pulled, total_new, total_updated


async def _pull_batch(
    base_url: str,
    auth_token: str,
    since: Optional[datetime | FederationFeedCursor],
    namespace_filter: Optional[List[str]],
    category_filter: Optional[List[str]],
    *,
    copy_embeddings: bool = False,
) -> Tuple[List[Dict[str, Any]], Optional[FederationFeedCursor], bool]:
    """HTTP GET one batch. Returns (memories, next_cursor, has_more).

    v6.1 F-1: when copy_embeddings=True, requests embedding bytes via
    the ?copy_embeddings=1 query param so the receiver can ingest
    pre-computed vectors without re-embedding.
    """
    url = base_url.rstrip("/") + "/v1/federation/feed"
    params: Dict[str, Any] = {"limit": FEDERATION_BATCH_LIMIT}
    if since is not None:
        if isinstance(since, FederationFeedCursor):
            params["since"] = _encode_feed_cursor(since.updated, since.memory_id)
        else:
            params["since"] = _encode_feed_cursor(since, FEDERATION_CURSOR_LOWER_ID)
    if namespace_filter:
        params["namespace"] = ",".join(namespace_filter)
    if category_filter:
        params["category"] = ",".join(category_filter)
    if copy_embeddings:
        params["copy_embeddings"] = "true"

    headers = {"Authorization": f"Bearer {auth_token}"}

    # F1 (adversarial review 2026-06-28): re-validate + DNS-pin (see _check_peer_schema).
    try:
        client, _ = await make_safe_client(
            url, timeout=FEDERATION_HTTP_TIMEOUT, allow_private=_federation_allow_private(),
        )
    except Exception as e:
        raise RuntimeError(f"federation URL rejected (SSRF/DNS): {type(e).__name__}: {e}") from e
    async with client:
        r = await client.get(url, params=params, headers=headers)
        if r.status_code == 401:
            raise RuntimeError("federation auth token rejected (401)")
        if r.status_code == 403:
            raise RuntimeError("federation auth insufficient role (403)")
        r.raise_for_status()
        body = r.json()

    memories = body.get("memories", []) or []
    next_cursor_raw = body.get("next_cursor")
    next_cursor = _decode_feed_cursor(next_cursor_raw) if next_cursor_raw else None
    has_more = bool(body.get("has_more"))
    return memories, next_cursor, has_more


async def pull_memory_by_id(
    base_url: str,
    auth_token: str,
    memory_id: str,
    namespace_filter: Optional[List[str]],
    category_filter: Optional[List[str]],
) -> List[Dict[str, Any]]:
    """Fetch one authorized memory through the explicit federation by-id path."""
    url = base_url.rstrip("/") + f"/v1/federation/memory/{memory_id}"
    params: Dict[str, Any] = {}
    if namespace_filter:
        params["namespace"] = ",".join(namespace_filter)
    if category_filter:
        params["category"] = ",".join(category_filter)
    headers = {"Authorization": f"Bearer {auth_token}"}

    # F1 (adversarial review 2026-06-28): re-validate + DNS-pin (see _check_peer_schema).
    try:
        client, _ = await make_safe_client(
            url, timeout=FEDERATION_HTTP_TIMEOUT, allow_private=_federation_allow_private(),
        )
    except Exception as e:
        raise RuntimeError(f"federation URL rejected (SSRF/DNS): {type(e).__name__}: {e}") from e
    async with client:
        r = await client.get(url, params=params, headers=headers)
        if r.status_code == 401:
            raise RuntimeError("federation auth token rejected (401)")
        if r.status_code == 403:
            raise RuntimeError("federation auth insufficient role (403)")
        if r.status_code == 404:
            return []
        r.raise_for_status()
        body = r.json()
    if isinstance(body, dict) and body.get("id") == memory_id:
        return [body]
    return []


async def _store_memories(
    repo: FederationRepository,
    tx: Transaction,
    peer_name: str,
    memories: List[Dict[str, Any]],
    backend: Optional[FederationBackend] = None,
) -> Tuple[int, int]:
    """Upsert a batch. Returns (newly_inserted, updated_existing).

    v6.1 F-1.4: when ``backend`` is provided, also pulls embedding bytes
    from inbound MemoryItem payloads when present + accepted by local
    model-match. The embedding is written via the existing memories
    repository ``upsert_memory_embedding`` so the join-table /
    direct-column shapes per backend are handled.
    """
    new_n = 0
    upd_n = 0
    # v6.1 F-1.4: model match preflight. Skip embedding when peer's model
    # doesn't match ours — store the row content as before.
    local_embed_model: Optional[str] = None
    embed_dim_expected: Optional[int] = None
    if backend is not None:
        try:
            from mnemos.core.config import embed_http_model_override, get_settings as _gs

            _s = _gs()
            # Prefer the HTTP-backend model env knob when active; fall back
            # to settings.providers.inference_embed_model. The settings
            # field is empty on instances that route embedding via HTTP
            # (MEDUSA edge replica points at MEDUSA :8090 bge-m3 via
            # MNEMOS_EMBED_HTTP_MODEL=bge-m3).
            local_embed_model = (
                embed_http_model_override()
                or (getattr(_s.providers, "inference_embed_model", "") or "").strip()
                or None
            )
            embed_dim_expected = getattr(_s.database, "embedding_dim", None)
        except Exception:
            local_embed_model = None
            embed_dim_expected = None
    for mem in memories:
        remote_id = mem.get("id")
        if not remote_id or not isinstance(remote_id, str):
            continue
        if mem.get("type") == "consolidation":
            upd_n += await _apply_consolidation_tombstone(repo, tx, peer_name, mem)
            continue
        # Cap inbound strings. A hostile peer otherwise fills the disk.
        content = _cap(mem.get("content", ""), FEDERATION_MAX_CONTENT)
        verbatim = _cap(
            mem.get("verbatim_content") or mem.get("content", ""),
            FEDERATION_MAX_CONTENT,
        )
        compressed = _cap(mem.get("compressed_content"), FEDERATION_MAX_CONTENT)
        category = _cap(mem.get("category", "federation"), FEDERATION_MAX_NAME)
        subcategory = _cap(mem.get("subcategory"), FEDERATION_MAX_NAME)
        namespace = _cap(mem.get("namespace") or "default", FEDERATION_MAX_NAME)
        local_id = f"{FEDERATION_ID_PREFIX}{peer_name}:{remote_id}"
        remote_updated = _coerce_datetime(mem.get("updated") or mem.get("created"))

        # Check existing
        existing = await repo.fetch_federated_memory_marker(tx, local_id)
        mutation_applied = False
        source_audit_provenance: dict[str, Any] = {}
        primary_eid = mem.get("audit_latest_entry_id")
        primary_hash = mem.get("audit_latest_entry_hash")
        if isinstance(primary_eid, str) and primary_eid:
            source_audit_provenance["federation_source_audit_latest_entry_id"] = primary_eid
        if isinstance(primary_hash, str) and primary_hash:
            source_audit_provenance["federation_source_audit_latest_entry_hash"] = primary_hash

        meta_raw = mem.get("metadata") or {}
        if isinstance(meta_raw, dict):
            meta_raw = {**meta_raw, "federation_remote_id": remote_id}
        else:
            meta_raw = {"federation_remote_id": remote_id}
        meta_raw.update(source_audit_provenance)
        meta_json = json.dumps(meta_raw)
        if len(meta_json) > FEDERATION_MAX_METADATA:
            # Drop metadata if it's absurdly large; keep the remote_id pointer.
            meta_raw = {
                "federation_remote_id": remote_id,
                "_metadata_truncated": True,
                **source_audit_provenance,
            }

        classified = classify_persisted_text_fields(
            content=content,
            verbatim_content=verbatim,
            compressed_content=compressed,
            metadata=meta_raw,
            namespace=namespace,
            classified_at="federation_pull",
            memory_id=local_id,
        )
        namespace = classified.namespace
        persisted_metadata = dict(classified.metadata)
        meta_json = json.dumps(persisted_metadata)
        if len(meta_json) > FEDERATION_MAX_METADATA:
            truncated_meta = {"federation_remote_id": remote_id, "_metadata_truncated": True}
            truncated_meta.update(source_audit_provenance)
            for key, value in classified.metadata.items():
                if str(key).startswith("secret_"):
                    truncated_meta[key] = value
            meta_json = json.dumps(truncated_meta)
            persisted_metadata = truncated_meta

        if existing is None:
            inserted = await repo.insert_federated_memory(
                tx,
                local_id=local_id,
                content=content,
                category=category,
                subcategory=subcategory,
                metadata_json=meta_json,
                verbatim_content=verbatim,
                quality_rating=mem.get("quality_rating") or 75,
                namespace=namespace,
                source_model=mem.get("source_model"),
                source_provider=mem.get("source_provider"),
                source_session=mem.get("source_session"),
                source_agent=mem.get("source_agent"),
                peer_name=peer_name,
                remote_updated=remote_updated,
            )
            if inserted:
                new_n += 1
                mutation_applied = True
                # NOTE: deliberately NOT 'continue' here — fall through to
                # the F-1.4 embedding-copy block at the bottom of the loop
                # so newly inserted rows get their embedding written same
                # as updated rows. Pre-F-1.4 this 'continue' lived here.
            else:
                # Concurrent create from another federation consumer (the
                # partial-fleet rollout window with queue-mode + legacy
                # durables is the canonical trigger; both consumers see
                # the same event, both pass the existence check, both attempt
                # to insert, only one wins). Re-fetch and fall through to the
                # update-when-newer branch so the losing path still applies
                # a delta if its remote_updated is the freshest.
                existing = await repo.fetch_federated_memory_marker(tx, local_id)

        if existing is not None:
            # Update only if the inbound remote_updated beats the
            # CURRENT row state — not the snapshot we read at the top
            # of this iteration. Without the WHERE-clause freshness
            # guard, two concurrent consumers handling events at
            # different remote_updated timestamps can both pass the
            # Python-side check on the same baseline and the older
            # one's write can commit second, rolling local state
            # backward to the older remote_updated. Codex round-3
            # audit (2026-05-01).
            if _coerce_datetime(existing["federation_remote_updated"]) is None or (
                remote_updated
                and _coerce_datetime(remote_updated)
                and _coerce_datetime(remote_updated) > _coerce_datetime(existing["federation_remote_updated"])
            ):
                updated = await repo.update_federated_memory_if_newer(
                    tx,
                    local_id=local_id,
                    content=content,
                    category=category,
                    subcategory=subcategory,
                    metadata_json=meta_json,
                    verbatim_content=verbatim,
                    quality_rating=mem.get("quality_rating") or 75,
                    namespace=namespace,
                    remote_updated=remote_updated,
                )
                # Count only rows actually affected. A concurrent newer event
                # commits first → our WHERE filter fails → 0 rows
                # → don't increment upd_n. The state in the row is
                # already as fresh as we have, and ON CONFLICT
                # idempotency means this is a successful no-op.
                if updated:
                    upd_n += 1
                    mutation_applied = True

        # v6.1 F-1.4: optional embedding copy. Only attempt when:
        #   1. backend was passed in (caller opted in)
        #   2. payload carries an embedding field (peer sent it via F-1.3)
        #   3. embedding_model matches local config (skip on drift; row stays)
        #   4. embedding_dim matches local-DB-expected dim
        # On any mismatch the memory content still lands; embedding column
        # stays NULL until a local re-embed worker fills it.
        if backend is not None:
            emb_b64 = mem.get("embedding")
            emb_model = mem.get("embedding_model")
            emb_dim = mem.get("embedding_dim")
            logger.info(
                "[federation/embed] check %s emb_b64_len=%d emb_model=%s emb_dim=%s local_model=%s local_dim=%s",
                local_id,
                len(emb_b64 or ""),
                emb_model,
                emb_dim,
                local_embed_model,
                embed_dim_expected,
            )
            if emb_b64 and emb_model:
                if local_embed_model and emb_model != local_embed_model:
                    logger.debug(
                        "[federation/embed] skip %s — peer model=%s != local %s",
                        local_id,
                        emb_model,
                        local_embed_model,
                    )
                elif embed_dim_expected and emb_dim and emb_dim != embed_dim_expected:
                    logger.debug(
                        "[federation/embed] skip %s — dim=%s != expected %s",
                        local_id,
                        emb_dim,
                        embed_dim_expected,
                    )
                else:
                    try:
                        import base64
                        import array as _array

                        raw_bytes = base64.b64decode(emb_b64)
                        arr = _array.array("f")
                        arr.frombytes(raw_bytes)
                        vec = list(arr)
                        logger.info(
                            "[federation/embed] decoded %s vec_len=%d expected=%s",
                            local_id,
                            len(vec),
                            embed_dim_expected,
                        )
                        if not embed_dim_expected or len(vec) == embed_dim_expected:
                            await backend.memories.upsert_memory_embedding(tx, local_id, vec)
                            logger.info("[federation/embed] upsert OK %s", local_id)
                        else:
                            logger.warning(
                                "[federation/embed] dim mismatch %s vec=%d expected=%d",
                                local_id,
                                len(vec),
                                embed_dim_expected,
                            )
                    except Exception:
                        logger.warning(
                            "[federation/embed] failed to decode/store %s",
                            local_id,
                            exc_info=True,
                        )

        # v6.2 M-2.2.1 federation audit write. Replica records each
        # applied inbound write under op="replicate" with writer_id="fed:<peer>"
        # so its local audit chain is universal across local API writes and
        # federation mutations. Source peers may publish their own audit head;
        # the receiver stores that as row provenance but never treats it as the
        # predecessor for this local `fed:<peer>:<remote>` replica chain.
        # Enforced audit writes make the first pull seed a local replica chain
        # and make later pulls fail closed if the local predecessor is corrupt.
        if mutation_applied and backend is not None and backend.audit_chain is not None:
            from mnemos.workers.audit_sealer import audit_chain_enabled

            if audit_chain_enabled():
                from mnemos.core.config import get_settings as _gs2

                _s = _gs2()
                _ss = (getattr(_s.server, "session_secret", "") or "").encode("utf-8")
                if _ss:
                    if primary_eid or primary_hash:
                        logger.info(
                            "[federation/audit] peer=%s memory=%s carrying source_chain_head eid=%s hash=%s",
                            peer_name,
                            local_id,
                            str(primary_eid or "")[:16],
                            str(primary_hash or "")[:16],
                        )

                    from mnemos.audit import write_audit_entry

                    await write_audit_entry(
                        backend,
                        tx,
                        op="replicate",
                        memory_id_str=local_id,
                        content=content,
                        category=category,
                        subcategory=subcategory,
                        metadata=persisted_metadata,
                        embedding=None,
                        writer_id=f"fed:{peer_name}",
                        session_secret=_ss,
                        enforce_continuity=True,
                    )

    return new_n, upd_n


async def _apply_consolidation_tombstone(
    repo: FederationRepository,
    tx: Transaction,
    peer_name: str,
    event: Dict[str, Any],
) -> int:
    remote_id = event.get("id")
    canonical_remote_id = event.get("consolidated_into")
    if not isinstance(remote_id, str) or not isinstance(canonical_remote_id, str):
        return 0
    raw_consolidated_at = event.get("consolidated_at")
    if isinstance(raw_consolidated_at, str):
        try:
            consolidated_at = datetime.fromisoformat(raw_consolidated_at.replace("Z", "+00:00"))
        except ValueError:
            consolidated_at = None
    else:
        consolidated_at = None
    local_id = f"{FEDERATION_ID_PREFIX}{peer_name}:{remote_id}"
    local_canonical_id = f"{FEDERATION_ID_PREFIX}{peer_name}:{canonical_remote_id}"
    updated = await repo.apply_consolidation_tombstone(
        tx,
        local_id=local_id,
        local_canonical_id=local_canonical_id,
        consolidated_at=consolidated_at,
        remote_id=remote_id,
        canonical_remote_id=canonical_remote_id,
        peer_name=peer_name,
    )
    return 1 if updated else 0


# ── Background worker ────────────────────────────────────────────────────────


async def federation_worker_loop(backend: FederationPersistence) -> None:
    """Background loop: iterate enabled peers, sync those whose interval has elapsed.

    Started from the FastAPI lifespan. Cancels cleanly on shutdown.
    """
    import asyncio

    logger.info("federation worker started")
    while True:
        try:
            await asyncio.sleep(60)  # check every minute
            async with backend.transactional() as tx:
                # Codex review-round-5 — order by computed next-due
                # time (last_sync_at + sync_interval), not last_sync_at
                # alone. Heterogeneous sync_interval_secs values mean a
                # long-interval peer's last_sync_at can be hours in the
                # past while still being LESS overdue than a short-
                # interval peer that just became due. The previous
                # `ORDER BY COALESCE(last_sync_at, epoch)` would have
                # let 10 long-interval transient-failing peers starve
                # short-interval healthy peers every 60s tick.
                due = await backend.federation.list_due_peers(tx)
            for p in due:
                try:
                    await sync_peer(backend, p["id"])
                except Exception:
                    logger.exception("federation: sync failed for peer %s", p["name"])
        except asyncio.CancelledError:
            logger.info("federation worker cancelled")
            raise
        except Exception:  # pragma: no cover
            logger.exception("federation worker iteration failed")
