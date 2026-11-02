"""Anti-entropy digests over a federation namespace (decision M12 item 4).

A replica cannot tell from its cursor alone whether it holds everything it was
supposed to: rows that left the source's sequence window before the replica
first pulled, or a replica row lost to a crash between fetch and commit, sit
outside the cursor and stay missing forever. An anti-entropy pass compares a
cheap summary of both sides instead of the corpus:

    /v1/federation/digest?namespace=&bucket=  ->  hash buckets over
                                                  (id, content_hash, updated)

Each row is placed in exactly one bucket by a SHA-256 of its id, so the split
is stable across nodes and independent of ordering. The bucket's value is a
Merkle root over its rows' leaves. The hashing is *the audit chain's*:
``mnemos.audit.crypto.merkle_leaf`` / ``merkle_root``, with the leaf preimage
being ``sha256(id || content_hash || updated)`` so the three columns an
operator cares about are all covered. One hashing scheme, one Merkle
implementation, no second one to keep in sync.

The puller (``mnemos.domain.anti_entropy``) diffs the two digest documents and
re-pulls only the rows of buckets whose roots differ, through the existing
authorized ``/v1/federation/memory/{id}`` path, so the feed's visibility and
redaction rules apply to the repair exactly as they do to a normal pull.

Note the term: these are *buckets*, not shards. Each bucket spans the whole
key space, so a diff names individual ids to refetch rather than a contiguous
range.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from mnemos.audit.crypto import merkle_leaf, merkle_root

DEFAULT_BUCKET_SIZE = 256
MIN_BUCKET_SIZE = 1
MAX_BUCKET_SIZE = 4096


class DigestError(ValueError):
    """A digest document is malformed and cannot be diffed."""


def _normalize_ts(value: Any) -> str:
    """Canonical timestamp text so two nodes format the same instant alike.

    Sources emit ISO strings (the feed wire format), Python datetimes (Sqlite
    rows come back as ``datetime``), or None. Both presentations of the same
    instant must hash identically or every bucket would differ.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        parsed = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        return parsed.isoformat().replace("+00:00", "Z")
    text = str(value).strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_bucket_size(bucket_size: int | None) -> int:
    """Clamp a requested width to the supported range (``None`` -> default).

    The width is echoed in the document and used for the labels, so both sides
    of a diff must agree on it: resolve it exactly once, up front.
    """
    return max(MIN_BUCKET_SIZE, int(bucket_size or DEFAULT_BUCKET_SIZE))


def bucket_index(memory_id: str, bucket_size: int = DEFAULT_BUCKET_SIZE) -> str:
    """Hex bucket label for ``memory_id`` — stable across nodes and orderings."""
    digest = hashlib.sha256(memory_id.encode("utf-8")).digest()
    index = int.from_bytes(digest[:8], "big") % bucket_size
    width = max(2, len(format(bucket_size - 1, "x")))
    return format(index, f"0{width}x")


def merkle_leaf_for_row(row: Mapping[str, Any]) -> bytes:
    """Leaf hash over ``(id, content_hash, updated)`` for one memory row."""
    preimage = hashlib.sha256()
    preimage.update(str(row.get("id") or "").encode("utf-8"))
    preimage.update(b"\x1f")
    preimage.update(str(row.get("content_hash") or "").encode("utf-8"))
    preimage.update(b"\x1f")
    preimage.update(_normalize_ts(row.get("updated")).encode("utf-8"))
    leaf_material = preimage.digest()
    return merkle_leaf(leaf_material, b"")


def build_namespace_digest(
    rows: Iterable[Mapping[str, Any]],
    *,
    bucket_size: int | None = DEFAULT_BUCKET_SIZE,
    namespace: str | None = None,
    id_prefix: str | None = None,
) -> dict[str, Any]:
    """Group ``rows`` into buckets and return one sorted digest *document*.

    The document is exactly what ``/v1/federation/digest`` returns and what
    ``diff_digests`` consumes — ``{namespace, bucket_size, row_count, buckets}``
    — so a caller never has to re-wrap the buckets to make them comparable.

    ``id_prefix`` strips a local replica key prefix (``fed:<peer>:``) so the
    ids in the document are the ids the *peer* knows the rows by — otherwise
    no two nodes would ever agree on a bucket. Rows without an id are skipped;
    missing ``content_hash`` is hashed as empty rather than dropped so a
    backfill that later fills it shows up as a difference instead of vanishing.
    """
    bucket_size = normalize_bucket_size(bucket_size)
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        memory_id = row.get("id")
        if not memory_id:
            continue
        remote_id = str(memory_id)
        if id_prefix and remote_id.startswith(id_prefix):
            remote_id = remote_id[len(id_prefix) :]
        normalized = {
            "id": remote_id,
            "content_hash": row.get("content_hash"),
            "updated": _normalize_ts(row.get("updated")),
        }
        grouped.setdefault(bucket_index(remote_id, bucket_size), []).append(normalized)

    buckets: list[dict[str, Any]] = []
    for bucket, members in sorted(grouped.items()):
        ordered = sorted(members, key=lambda item: item["id"])
        leaves = [merkle_leaf_for_row(item) for item in ordered]
        buckets.append(
            {
                "bucket": bucket,
                "count": len(ordered),
                "merkle_root": merkle_root(leaves).hex(),
                "ids": [item["id"] for item in ordered],
            }
        )
    return {
        "namespace": namespace,
        "bucket_size": bucket_size,
        "row_count": sum(b["count"] for b in buckets),
        "buckets": buckets,
    }


def filter_digest_by_namespace(
    buckets: Sequence[Mapping[str, Any]],
    ids: Iterable[str],
    *,
    bucket_size: int = DEFAULT_BUCKET_SIZE,
) -> list[dict[str, Any]]:
    """Re-fold a digest down to ``ids`` (the repair set).

    Used to build the expectation for the repair round: the same hashing rules
    must apply, so the caller compares like with like rather than hand-rolling
    a second projection.
    """
    wanted = {str(memory_id) for memory_id in ids}
    per_bucket: dict[str, list[str]] = {}
    for memory_id in sorted(wanted):
        per_bucket.setdefault(bucket_index(memory_id, bucket_size), []).append(memory_id)
    out: list[dict[str, Any]] = []
    by_bucket = {b["bucket"]: b for b in buckets}
    for bucket, members in sorted(per_bucket.items()):
        source = by_bucket.get(bucket)
        if source is None:
            out.append({"bucket": bucket, "count": 0, "merkle_root": None, "ids": []})
            continue
        kept = [mid for mid in source.get("ids", []) if mid in wanted]
        out.append({"bucket": bucket, "count": len(kept), "merkle_root": source.get("merkle_root"), "ids": kept})
    return out


def _bucket_map(document: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(document, Mapping):
        raise DigestError("digest document must be an object")
    buckets = document.get("buckets")
    if not isinstance(buckets, list):
        raise DigestError("digest document missing buckets")
    index: dict[str, dict[str, Any]] = {}
    for entry in buckets:
        if not isinstance(entry, Mapping) or "bucket" not in entry:
            raise DigestError("digest bucket entry malformed")
        index[str(entry["bucket"])] = dict(entry)
    return index


def diff_digests(source: Mapping[str, Any], replica: Mapping[str, Any]) -> dict[str, Any]:
    """Compare two digest documents bucket by bucket.

    Returns the buckets that differ plus the per-side id sets inside them:

      * ``source_only_bucket_ids`` — rows the source has that the replica lacks
        (the lost-row case: a replica bucket whose root is MISSING, or whose
        membership is short).
      * ``replica_only_bucket_ids`` — rows the replica holds that the source no
        longer publishes (withdrawn, archived, permission-narrowed).
      * ``repair_ids`` — bucket membership from the source for every differing
        bucket, i.e. the ids to re-pull.

    A differing root with identical membership means the *content* moved
    (same ids, new ``content_hash``/``updated``), so the ids are re-pulled
    too — the feed's freshness rule then decides whether to apply them.
    """
    source_buckets = _bucket_map(source)
    replica_buckets = _bucket_map(replica)
    differing = sorted(set(source_buckets) | set(replica_buckets))
    differing = [
        bucket
        for bucket in differing
        if (source_buckets.get(bucket) or {}).get("merkle_root")
        != (replica_buckets.get(bucket) or {}).get("merkle_root")
    ]

    source_only: list[str] = []
    replica_only: list[str] = []
    repair_ids: list[str] = []
    for bucket in differing:
        src = source_buckets.get(bucket) or {}
        rep = replica_buckets.get(bucket) or {}
        src_ids = [str(mid) for mid in src.get("ids") or []]
        rep_ids = [str(mid) for mid in rep.get("ids") or []]
        # Hoist the membership sets: rebuilding them per element made the
        # per-bucket scan quadratic in the bucket's width.
        src_set = set(src_ids)
        rep_set = set(rep_ids)
        source_only.extend(mid for mid in src_ids if mid not in rep_set)
        replica_only.extend(mid for mid in rep_ids if mid not in src_set)
        repair_ids.extend(src_ids)

    return {
        "differing_buckets": differing,
        "source_only_bucket_ids": source_only,
        "replica_only_bucket_ids": replica_only,
        "repair_ids": repair_ids,
        "in_sync": not differing,
    }
