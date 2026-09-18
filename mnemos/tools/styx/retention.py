"""Grandfather-father-son retention for STYX artifacts on Drive.

The policy keeps the newest artifact in each of the most recent N day
buckets, W ISO-week buckets, and M month buckets; everything else is
pruned. Bucketing (rather than counting files) means a day that produced
three runs still consumes exactly one daily slot, so a flapping timer cannot
quietly evict the operator's entire history.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

#: ``mnemos-<host>-<UTC timestamp>.mif.tar.gz.age``
#:
#: The timestamp is the sort key and the bucket key, so it is parsed back out
#: of the name rather than trusted from Drive's own metadata: Drive's
#: ``createdTime`` reflects when the upload happened, which drifts from the
#: backup's logical date whenever a run is retried or backfilled.
ARTIFACT_SUFFIX = ".mif.tar.gz.age"
_STAMP = re.compile(r"-(\d{8}T\d{6}Z)" + re.escape(ARTIFACT_SUFFIX) + r"$")
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def artifact_name(host_label: str, moment: datetime) -> str:
    """Build the canonical artifact name for a run.

    Every run gets its own timestamped name. STYX never overwrites an
    existing artifact in place: an upload that fails partway through must not
    be able to destroy the last known-good backup.
    """
    stamp = moment.astimezone(timezone.utc).strftime(_STAMP_FORMAT)
    return f"mnemos-{host_label}-{stamp}{ARTIFACT_SUFFIX}"


def parse_artifact_time(name: str) -> datetime | None:
    """Recover the UTC timestamp from an artifact name, or ``None``."""
    match = _STAMP.search(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), _STAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass(frozen=True)
class RemoteArtifact:
    """A STYX artifact as Drive reports it."""

    file_id: str
    name: str
    size: int = 0
    #: Drive's server-computed MD5. Present on rows read back from the API,
    #: absent on artifacts constructed locally in tests.
    md5: str | None = None

    @property
    def timestamp(self) -> datetime | None:
        return parse_artifact_time(self.name)


def _bucket_keys(moment: datetime) -> tuple[str, str, str]:
    iso_year, iso_week, _ = moment.isocalendar()
    return (
        moment.strftime("%Y-%m-%d"),
        f"{iso_year}-W{iso_week:02d}",
        moment.strftime("%Y-%m"),
    )


def plan_retention(
    artifacts: list[RemoteArtifact],
    *,
    daily: int,
    weekly: int,
    monthly: int,
) -> tuple[list[RemoteArtifact], list[RemoteArtifact]]:
    """Split ``artifacts`` into ``(keep, prune)`` under the GFS policy.

    Artifacts whose names carry no parsable timestamp are always kept. STYX
    is not the only thing that might ever write to the operator's backup
    folder, and a retention pass is the wrong place to be creative about
    files it does not recognise.
    """
    recognised: list[tuple[datetime, RemoteArtifact]] = []
    keep: list[RemoteArtifact] = []
    for art in artifacts:
        moment = art.timestamp
        if moment is None:
            keep.append(art)
        else:
            recognised.append((moment, art))

    # Newest first, so the first artifact seen in a bucket is the one kept.
    recognised.sort(key=lambda pair: pair[0], reverse=True)

    limits = (max(0, daily), max(0, weekly), max(0, monthly))
    seen: tuple[dict[str, str], ...] = ({}, {}, {})
    keep_ids: set[str] = set()

    for moment, art in recognised:
        for tier, key in enumerate(_bucket_keys(moment)):
            bucket = seen[tier]
            if key in bucket:
                continue
            if len(bucket) >= limits[tier]:
                continue
            bucket[key] = art.file_id
            keep_ids.add(art.file_id)

    prune = [art for _, art in recognised if art.file_id not in keep_ids]
    keep.extend(art for _, art in recognised if art.file_id in keep_ids)
    return keep, prune
