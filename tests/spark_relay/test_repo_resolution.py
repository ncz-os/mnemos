"""Spark relay repo-resolution + offload-eligibility tests.

Regression coverage for the 2026-06-16 bug where every ``build:<repo>`` job the
enqueuer offloaded to the Spark hit "no repo mapping for kind" — the relay only
knew the legacy colon-prefixed project kinds, not the home-fleet ``build:<repo>``
convention. Pure unit tests: no GCS / network / NGC needed.
"""

import pytest

from spark_relay.enqueuer import _spark_should_offload
from spark_relay.spark_poller import repo_url_for_kind, repo_aliases


class TestRepoUrlForKind:
    def test_build_prefix_resolves_known_repo(self):
        assert repo_url_for_kind("build:mnemos") == "https://gitlab.com/mnemos-os/mnemos.git"
        assert repo_url_for_kind("build:riskyeats") == "https://gitlab.com/perlowja/riskyeats.git"
        assert repo_url_for_kind("build:zeroclaw") == "https://gitlab.com/nclawzero/zeroclaw.git"

    def test_build_prefix_strips_trailing_kind_segment(self):
        # build:<repo>:<extra> -> the repo token is just the first segment.
        assert repo_url_for_kind("build:mnemos:phase1") == "https://gitlab.com/mnemos-os/mnemos.git"

    def test_build_prefix_unknown_repo_is_unmapped(self):
        # SSRF-safe: an unknown suffix must NOT resolve (e.g. the misrouted
        # build:pantheon-security kind that started this whole incident).
        assert repo_url_for_kind("build:pantheon-security") is None
        assert repo_url_for_kind("build:totally-unknown") is None

    def test_legacy_colon_kinds_still_resolve(self):
        assert repo_url_for_kind("mnemos:fix-something") == "https://gitlab.com/mnemos-os/mnemos.git"
        assert repo_url_for_kind("ic-engine:patch") == "https://gitlab.com/argonautsystems/ic-engine.git"

    def test_unmapped_kind_returns_none(self):
        assert repo_url_for_kind("research:market") is None
        assert repo_url_for_kind("") is None
        assert repo_url_for_kind(None) is None

    def test_env_allowlist_extends_build_resolution(self, monkeypatch):
        monkeypatch.setenv("SPARK_REPO_ALLOWLIST", "widget=https://example.com/acme/widget.git")
        assert "widget" in repo_aliases()
        assert repo_url_for_kind("build:widget") == "https://example.com/acme/widget.git"


class TestSparkShouldOffload:
    def test_mappable_build_job_is_offloaded(self):
        assert _spark_should_offload({"kind": "build:mnemos"}) is True
        assert _spark_should_offload({"kind": "build:riskyeats"}) is True

    def test_unmappable_build_job_is_released(self):
        # The bug's blast radius: an unmappable build job must NOT be offloaded
        # to the Spark (it would zombie / degrade to a chat suggestion).
        assert _spark_should_offload({"kind": "build:pantheon-security"}) is False

    def test_noncommit_kind_offloaded_without_repo(self):
        # Research/analysis/triage are answered via chat — no repo required.
        assert _spark_should_offload({"kind": "research:foo"}) is True
        assert _spark_should_offload({"kind": "analysis:bar"}) is True

    def test_explicit_spark_host_target_always_offloaded(self):
        # Operator deliberately routed it to the Spark; honor it even if unmapped.
        assert _spark_should_offload(
            {"kind": "build:anything", "eligible_hosts": ["spark-0c53"]}
        ) is True

    def test_unmapped_unhosted_commit_kind_released(self):
        assert _spark_should_offload({"kind": "build:unknown"}) is False
        assert _spark_should_offload({"kind": "fix:somerepo"}) is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
