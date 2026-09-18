"""STYX retention policy — grandfather-father-son bucketing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mnemos.tools.styx.retention import (
    RemoteArtifact,
    artifact_name,
    parse_artifact_time,
    plan_retention,
)


def _art(moment: datetime, host: str = "pythia") -> RemoteArtifact:
    name = artifact_name(host, moment)
    return RemoteArtifact(file_id=name, name=name)


def test_artifact_name_round_trips_through_the_parser():
    moment = datetime(2026, 9, 17, 2, 40, 0, tzinfo=timezone.utc)
    name = artifact_name("pythia", moment)
    assert name == "mnemos-pythia-20260917T024000Z.mif.tar.gz.age"
    assert parse_artifact_time(name) == moment


def test_artifact_name_normalises_to_utc():
    """A host in a non-UTC zone must still bucket alongside everyone else."""
    eastern = timezone(timedelta(hours=-4))
    local = datetime(2026, 9, 16, 22, 40, 0, tzinfo=eastern)
    assert artifact_name("pythia", local) == "mnemos-pythia-20260917T024000Z.mif.tar.gz.age"


@pytest.mark.parametrize(
    "name",
    [
        "not-a-styx-artifact.txt",
        "mnemos-pythia-nonsense.mif.tar.gz.age",
        "mnemos-pythia-20261301T024000Z.mif.tar.gz.age",  # month 13
    ],
)
def test_parse_artifact_time_rejects_junk(name):
    assert parse_artifact_time(name) is None


def test_plan_retention_keeps_the_newest_per_bucket():
    base = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)
    artifacts = [_art(base - timedelta(days=n)) for n in range(40)]

    keep, prune = plan_retention(artifacts, daily=7, weekly=4, monthly=2)

    assert len(keep) + len(prune) == len(artifacts)
    assert not (set(a.file_id for a in keep) & set(a.file_id for a in prune))
    # The newest is never pruned. That is the one that just succeeded.
    assert artifacts[0].file_id in {a.file_id for a in keep}
    # 7 dailies + at most 4 more weeklies + at most 2 more monthlies.
    assert 7 <= len(keep) <= 13


def test_plan_retention_treats_a_day_as_one_slot_not_one_file():
    """Several runs in one day must not evict the rest of the history.

    A flapping timer that fired hourly would otherwise consume every daily
    slot in an afternoon and silently prune weeks of good backups.
    """
    day = datetime(2026, 9, 17, 0, 0, tzinfo=timezone.utc)
    same_day = [_art(day + timedelta(hours=h)) for h in range(12)]
    older = [_art(day - timedelta(days=n)) for n in range(1, 6)]

    keep, _ = plan_retention(same_day + older, daily=5, weekly=0, monthly=0)

    kept_days = {a.timestamp.date() for a in keep}
    assert len(kept_days) == 5, "five distinct days should be retained, not five files from one"
    # And within the busy day, the newest run is the survivor.
    newest_that_day = max(same_day, key=lambda a: a.timestamp)
    assert newest_that_day.file_id in {a.file_id for a in keep}


def test_plan_retention_never_prunes_unrecognised_files():
    """The backup folder is the operator's, not STYX's to tidy up."""
    stranger = RemoteArtifact(file_id="x", name="operator-notes.txt")
    mine = _art(datetime(2026, 9, 17, tzinfo=timezone.utc))

    keep, prune = plan_retention([stranger, mine], daily=1, weekly=0, monthly=0)

    assert stranger in keep
    assert prune == []


def test_plan_retention_scopes_nothing_when_limits_are_zero():
    """All-zero limits prune everything recognised — but still nothing else."""
    base = datetime(2026, 9, 17, tzinfo=timezone.utc)
    artifacts = [_art(base - timedelta(days=n)) for n in range(3)]

    keep, prune = plan_retention(artifacts, daily=0, weekly=0, monthly=0)

    assert keep == []
    assert len(prune) == 3
