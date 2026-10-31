"""Anti-entropy repair for federation replicas (decision M12 item 4).

``sync_peer`` only ever moves forward along the peer's feed cursor, so it is
blind by construction to rows it *should* have but never saw: rows that left
the source's sequence window before the replica's first pull, or replica rows
lost after they were committed. The cursor cannot report a hole behind it.

This module closes that hole by comparison instead of replay:

  1. Ask the source for ``/v1/federation/digest`` (hash buckets over
     ``(id, content_hash, updated)`` — see ``replication_digest``).
  2. Build the same digest from the replica's own rows for that peer.
  3. Diff (``replication_digest.diff_digests``) and re-pull ONLY the ids of the
     buckets that differ, through the authorized by-id endpoint
     (``pull_memory_by_id``) so the feed's visibility and redaction rules
     govern the repair exactly as they govern a normal pull.
  4. Apply through the same ``_store_memories`` upsert a normal sync uses, so
     freshness guards, journal fencing and embedding gating behave identically.

Repair never touches the replica's sync cursor: the re-pulled rows are outside
the feed's sequence order and advancing the cursor to them would skip every
real event between.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

import httpx

from mnemos.core.safe_http import make_safe_client
from mnemos.domain.federation import (
    FEDERATION_HTTP_TIMEOUT,
    FederationBackend,
    _federation_allow_private,
    _store_memories,
    pull_memory_by_id,
)
from mnemos.domain.replication_digest import (
    DEFAULT_BUCKET_SIZE,
    build_namespace_digest,
    diff_digests,
    normalize_bucket_size,
)

logger = logging.getLogger(__name__)

# Bound a single repair: a corrupt/forged digest claiming the entire corpus is
# a full re-pull, which is exactly the load the operator did not ask for.
MAX_REPAIR_IDS = 5000


class AntiEntropyError(Exception):
    """The digest exchange itself failed (network, auth, malformed document)."""


def _local_replica_key(peer_name: str) -> str:
    return f"fed:{peer_name}:"


async def _fetch_peer_digest(
    base_url: str,
    auth_token: str,
    *,
    namespace: Optional[str] = None,
    bucket_size: int = DEFAULT_BUCKET_SIZE,
    client: Any | None = None,
) -> Dict[str, Any]:
    url = base_url.rstrip("/") + "/v1/federation/digest"
    # Echo the resolved width, not the caller's raw argument: the peer buckets
    # by `bucket` and reports that same number in the document it returns, so
    # an omitted width has to mean the shared default on both sides.
    params: Dict[str, Any] = {"bucket": normalize_bucket_size(bucket_size)}
    if namespace:
        params["namespace"] = namespace
    headers = {"Authorization": f"Bearer {auth_token}"}
    owns_client = client is None
    if owns_client:
        try:
            client, _ = await make_safe_client(
                url,
                timeout=FEDERATION_HTTP_TIMEOUT,
                allow_private=_federation_allow_private(),
            )
        except Exception as e:
            raise AntiEntropyError(f"federation URL rejected (SSRF/DNS): {type(e).__name__}: {e}") from e
    try:
        resp = await client.get(url, params=params, headers=headers)
        if resp.status_code == 404:
            raise AntiEntropyError("peer does not expose /v1/federation/digest (pre-M12 build)")
        if resp.status_code == 401:
            raise AntiEntropyError("federation auth token rejected (401)")
        if resp.status_code == 403:
            raise AntiEntropyError("federation auth insufficient role (403)")
        resp.raise_for_status()
        document = resp.json()
    finally:
        if owns_client:
            await client.aclose()
    if not isinstance(document, dict):
        raise AntiEntropyError("peer digest response must be a JSON object")
    document.setdefault("buckets", [])
    return document


async def build_local_digest(
    backend: FederationBackend,
    peer_id: str,
    *,
    namespace: Optional[str] = None,
    bucket_size: int = DEFAULT_BUCKET_SIZE,
) -> Dict[str, Any]:
    """Digest over what this replica actually holds for ``peer_id``."""
    repo = backend.federation
    async with backend.transactional() as tx:
        peer = await repo.get_peer(tx, peer_id)
        if not peer:
            raise AntiEntropyError(f"peer {peer_id} not found")
        rows = await repo.list_federated_memory_hashes(
            tx,
            peer_name=peer["name"],
            namespace=namespace,
        )
    # build_namespace_digest already returns the wire document shape, so both
    # sides of the diff are built by one function with one bucketing rule.
    return build_namespace_digest(
        rows,
        bucket_size=bucket_size,
        namespace=namespace,
        id_prefix=_local_replica_key(peer["name"]),
    )


async def repair_namespace(
    backend: FederationBackend,
    peer_id: str,
    diff: Mapping[str, Any],
    *,
    namespace: Optional[str] = None,
    max_ids: int = MAX_REPAIR_IDS,
) -> Dict[str, Any]:
    """Re-pull the ids in a digest diff. Returns a small operator report.

    ``diff`` is a ``diff_digests`` result. Ids are de-duplicated and capped at
    ``max_ids``; a capped repair reports ``truncated`` so the operator knows a
    second pass is needed rather than believing the replica is whole.
    """
    repo = backend.federation
    async with backend.transactional() as tx:
        peer = await repo.get_peer(tx, peer_id)
    if not peer:
        raise AntiEntropyError(f"peer {peer_id} not found")

    repair_ids: List[str] = list(dict.fromkeys(str(mid) for mid in diff.get("repair_ids") or []))
    truncated = len(repair_ids) > max_ids
    repair_ids = repair_ids[:max_ids]

    pulled = 0
    restored = 0
    failed: List[str] = []
    for remote_id in repair_ids:
        try:
            memories = await pull_memory_by_id(
                peer["base_url"],
                peer["auth_token"],
                remote_id,
                peer.get("namespace_filter"),
                peer.get("category_filter"),
            )
        except (httpx.HTTPError, RuntimeError) as e:
            logger.warning("federation anti-entropy: fetch %s failed: %s", remote_id, e)
            failed.append(remote_id)
            continue
        if not memories:
            # The row is no longer published (withdrawn/archived/narrowed).
            # That is a legitimate replica-only difference, not a failure.
            continue
        async with backend.transactional() as tx:
            new_n, upd_n = await _store_memories(repo, tx, peer["name"], memories, backend=backend)
        pulled += len(memories)
        restored += new_n + upd_n

    report = {
        "peer": peer["name"],
        "namespace": namespace,
        "checked_ids": len(repair_ids),
        "pulled": pulled,
        "restored": restored,
        "failed": failed,
        "truncated": truncated,
        "repaired_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    logger.info(
        "federation anti-entropy: peer=%s checked=%d pulled=%d restored=%d failed=%d truncated=%s",
        peer["name"],
        report["checked_ids"],
        pulled,
        restored,
        len(failed),
        truncated,
    )
    return report


async def run_anti_entropy(
    backend: FederationBackend,
    peer_id: str,
    *,
    namespace: Optional[str] = None,
    bucket_size: int = DEFAULT_BUCKET_SIZE,
    client: Any | None = None,
) -> Dict[str, Any]:
    """Fetch → diff → repair in one call."""
    repo = backend.federation
    async with backend.transactional() as tx:
        peer = await repo.get_peer(tx, peer_id)
    if not peer:
        raise AntiEntropyError(f"peer {peer_id} not found")
    source = await _fetch_peer_digest(
        peer["base_url"],
        peer["auth_token"],
        namespace=namespace,
        bucket_size=bucket_size,
        client=client,
    )
    replica = await build_local_digest(backend, peer_id, namespace=namespace, bucket_size=bucket_size)
    diff = diff_digests(source, replica)
    report = await repair_namespace(backend, peer_id, diff, namespace=namespace)
    report["diff"] = {k: v for k, v in diff.items() if k != "repair_ids"}
    return report
