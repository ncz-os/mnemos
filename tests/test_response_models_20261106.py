"""FB445 — response model shapes for ``mnemos.domain.models``.

Pins ``ConsultationRequest``, ``StatsResponse`` and ``HealthResponse`` on
``master`` (``22a3f7d8``): exact field sets, documented defaults, unknown keys
ignored, and lossless serialisation.

Pinned divergences, not endorsed: ``StatsResponse`` counters are plain ints and
floats with no lower bound, so negative values validate (the brief's
"non-negative" premise is false); and ``ConsultationRequest.tier`` is a
free-form string despite its comment naming three tiers.

Pure model construction: no backend, no I/O.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mnemos.domain.models import (
    SUPPORTED_CONSULTATION_MODES,
    ConsultationRequest,
    HealthResponse,
    StatsResponse,
)

CONSULTATION_FIELDS = {
    "prompt",
    "task_type",
    "mode",
    "limit_chars",
    "format",
    "models",
    "providers",
    "tier",
}

STATS_FIELDS = {
    "total_memories",
    "native_memories",
    "federated_memories",
    "memories_by_peer",
    "total_compressions",
    "average_compression_ratio",
    "average_quality_rating",
    "memories_by_category",
    "memories_by_subcategory",
    "unreviewed_compressions",
    "timestamp",
    # Replica mode (2026-11-22): the serving role, the standby's replay lag and
    # the embedding model identity ride /stats and /health.
    "role",
    "replay_lag_seconds",
    "embedding_model",
}

HEALTH_FIELDS = {
    "status",
    "timestamp",
    "database_connected",
    "version",
    "profile",
    "distillation_worker",
    "deletion_request_worker",
    "hard_deletion_request_worker",
    "persephone_archival_worker",
    "nats_publishing_enabled",
    "persistence_backend",
    "persistence_capabilities",
    "persistence_capability_details",
    # Replica mode (2026-11-22): see STATS_FIELDS.
    "role",
    "replay_lag_seconds",
    "embedding_model",
}

MIN_CONSULTATION = {"prompt": "hello"}
MIN_STATS = {
    "total_memories": 3,
    "total_compressions": 1,
    "average_compression_ratio": 0.5,
    "average_quality_rating": 4,
    "memories_by_category": {"facts": 3},
    "unreviewed_compressions": 0,
    "timestamp": "2026-11-06T12:00:00",
}
MIN_HEALTH = {
    "status": "ok",
    "timestamp": "2026-11-06T12:00:00",
    "database_connected": True,
    "version": "6.2.0",
}


# --------------------------------------------------------------------------- #
# Field sets
# --------------------------------------------------------------------------- #
def test_consultation_request_field_set():
    assert set(ConsultationRequest.model_fields) == CONSULTATION_FIELDS


def test_stats_response_field_set():
    assert set(StatsResponse.model_fields) == STATS_FIELDS


def test_health_response_field_set():
    assert set(HealthResponse.model_fields) == HEALTH_FIELDS


# --------------------------------------------------------------------------- #
# ConsultationRequest
# --------------------------------------------------------------------------- #
def test_consultation_defaults():
    req = ConsultationRequest(**MIN_CONSULTATION)
    assert req.task_type == "reasoning"
    assert req.mode == "auto"
    assert req.limit_chars is None
    assert req.format == "full"
    assert req.models is None
    assert req.providers is None
    assert req.tier is None


def test_consultation_prompt_is_required():
    with pytest.raises(ValidationError) as caught:
        ConsultationRequest()
    assert any(err["loc"] == ("prompt",) for err in caught.value.errors())


@pytest.mark.parametrize("mode", SUPPORTED_CONSULTATION_MODES)
def test_every_supported_consultation_mode_is_accepted(mode):
    assert ConsultationRequest(prompt="x", mode=mode).mode == mode


def test_unknown_consultation_mode_is_refused():
    with pytest.raises(ValidationError):
        ConsultationRequest(prompt="x", mode="telepathy")


def test_consultation_unknown_keys_are_ignored():
    req = ConsultationRequest(prompt="x", context="removed in #179", future_field=1)
    assert not hasattr(req, "context")
    assert "context" not in req.model_dump()
    assert "future_field" not in req.model_dump()


def test_consultation_format_accepts_explicit_none():
    assert ConsultationRequest(prompt="x", format=None).format is None


def test_consultation_selectors_keep_lists():
    req = ConsultationRequest(prompt="x", models=["claude", "openai"], providers=["eih"], tier="frontier")
    assert req.models == ["claude", "openai"]
    assert req.providers == ["eih"]
    assert req.tier == "frontier"


def test_consultation_tier_is_free_form():
    """Pinned divergence: the comment lists three tiers; any string validates."""
    assert ConsultationRequest(prompt="x", tier="whatever").tier == "whatever"


def test_consultation_round_trips():
    req = ConsultationRequest(prompt="x", mode="debate", limit_chars=100, models=["m"])
    assert ConsultationRequest(**req.model_dump()) == req
    assert ConsultationRequest.model_validate_json(req.model_dump_json()) == req


# --------------------------------------------------------------------------- #
# StatsResponse
# --------------------------------------------------------------------------- #
def test_stats_defaults():
    stats = StatsResponse(**MIN_STATS)
    assert stats.native_memories == 0
    assert stats.federated_memories == 0
    assert stats.memories_by_peer == {}
    assert stats.memories_by_subcategory == {}


@pytest.mark.parametrize(
    "missing",
    [
        "total_memories",
        "total_compressions",
        "average_compression_ratio",
        "average_quality_rating",
        "memories_by_category",
        "unreviewed_compressions",
        "timestamp",
    ],
)
def test_stats_required_fields_are_named(missing):
    payload = {key: value for key, value in MIN_STATS.items() if key != missing}
    with pytest.raises(ValidationError) as caught:
        StatsResponse(**payload)
    assert any(err["loc"] == (missing,) for err in caught.value.errors())


def test_stats_accepts_negative_counters():
    """Pinned divergence: no ge=0 bound, so negative counters validate."""
    stats = StatsResponse(
        **{
            **MIN_STATS,
            "total_memories": -1,
            "unreviewed_compressions": -5,
            "average_compression_ratio": -0.25,
            "average_quality_rating": -2,
            "memories_by_peer": {"peer": -3},
        }
    )
    assert stats.total_memories == -1
    assert stats.unreviewed_compressions == -5
    assert stats.average_compression_ratio == -0.25
    assert stats.memories_by_peer == {"peer": -3}


def test_stats_mapping_defaults_are_not_shared():
    first = StatsResponse(**MIN_STATS)
    second = StatsResponse(**MIN_STATS)
    first.memories_by_peer["peer"] = 1
    first.memories_by_subcategory.setdefault("facts", {})["x"] = 1
    assert second.memories_by_peer == {}
    assert second.memories_by_subcategory == {}


def test_stats_unknown_keys_are_ignored():
    stats = StatsResponse(**MIN_STATS, extra_counter=9)
    assert "extra_counter" not in stats.model_dump()


def test_stats_ratio_accepts_int_and_coerces_to_float():
    stats = StatsResponse(**{**MIN_STATS, "average_compression_ratio": 1})
    assert stats.average_compression_ratio == 1.0
    assert isinstance(stats.average_compression_ratio, float)


def test_stats_round_trips():
    stats = StatsResponse(**MIN_STATS, memories_by_subcategory={"facts": {"x": 1}})
    assert StatsResponse(**stats.model_dump()) == stats
    assert StatsResponse.model_validate_json(stats.model_dump_json()) == stats


# --------------------------------------------------------------------------- #
# HealthResponse
# --------------------------------------------------------------------------- #
def test_health_defaults():
    health = HealthResponse(**MIN_HEALTH)
    assert health.profile == "edge"
    assert health.distillation_worker is None
    assert health.deletion_request_worker is None
    assert health.hard_deletion_request_worker is None
    assert health.persephone_archival_worker is None
    assert health.nats_publishing_enabled is False
    assert health.persistence_backend is None
    assert health.persistence_capabilities == []
    assert health.persistence_capability_details == []


@pytest.mark.parametrize("missing", ["status", "timestamp", "database_connected", "version"])
def test_health_required_fields_are_named(missing):
    payload = {key: value for key, value in MIN_HEALTH.items() if key != missing}
    with pytest.raises(ValidationError) as caught:
        HealthResponse(**payload)
    assert any(err["loc"] == (missing,) for err in caught.value.errors())


def test_health_capability_defaults_are_not_shared():
    first = HealthResponse(**MIN_HEALTH)
    second = HealthResponse(**MIN_HEALTH)
    first.persistence_capabilities.append("vector")
    assert second.persistence_capabilities == []


def test_health_unknown_keys_are_ignored():
    health = HealthResponse(**MIN_HEALTH, uptime_seconds=12)
    assert "uptime_seconds" not in health.model_dump()


def test_health_round_trips():
    health = HealthResponse(
        **MIN_HEALTH,
        profile="core",
        nats_publishing_enabled=True,
        persistence_capabilities=["vector"],
    )
    assert HealthResponse(**health.model_dump()) == health
    assert HealthResponse.model_validate_json(health.model_dump_json()) == health
