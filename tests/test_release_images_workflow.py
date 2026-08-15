"""Verify the release-images workflow pins add-on overlays to known SHAs.

The release workflow (``/.github/workflows/release-images.yml``) must
fetch each add-on (pantheon/knemon/graeae/charon) at a known SHA so a
``vX.Y.Z`` tag can be reproduced from the lock file
(``.github/addons.lock.json``). These tests pin:

* the lock file lists one entry per overlay with a 40-character hex
  commit id (or the documented placeholder),
* the workflow branch on a tag push fetches the lock entry detached
  and refuses to fall back to ``ADDON_REF`` when the lock is missing,
* the resolved SHA is recorded in OCI image labels.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = REPO_ROOT / ".github" / "addons.lock.json"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release-images.yml"
ADDONS = ("pantheon", "knemon", "graeae", "charon")


def _read_lock() -> dict:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def _read_workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_addons_lock_lists_every_overlay():
    """The lock must declare every add-on overlay so a stray PR cannot
    ship a tag without pinning one of them."""
    lock = _read_lock()
    for name in ADDONS:
        assert name in lock, f".github/addons.lock.json missing entry for {name}"
        sha = lock[name]
        # Either a real 40-char hex SHA or the documented placeholder
        # (``00000...000``); the workflow treats the placeholder the same
        # as any other value (it still fetches and pins detached).
        assert re.fullmatch(r"[0-9a-f]{40}", sha), (
            f"addon {name!r} sha {sha!r} is not a 40-character hex commit id"
        )


def test_release_workflow_uses_lock_on_tag_push():
    """A tagged ``v*.*.*`` push must consult ``.github/addons.lock.json``
    rather than the mutable ``ADDON_REF`` branch.
    """
    workflow = _read_workflow()
    # The "Stage add-on wheels" step is the only place add-on SHAs are
    # resolved; pin its behaviour so a regression that drops the lock
    # fallback surfaces here.
    assert ".github/addons.lock.json" in workflow, (
        "release-images.yml no longer references the per-overlay SHA lock"
    )
    assert "GITHUB_REF_TYPE" in workflow, (
        "release-images.yml no longer branches on tag vs non-tag pushes"
    )


def test_release_workflow_refuses_tag_without_lock_file():
    """A tagged push whose checkout lacks ``.github/addons.lock.json`` must
    fail the build rather than fall back to the mutable ``ADDON_REF`` ref.
    Publishing a version tag built from the branch tip would defeat the
    reproducibility contract the lock exists to provide: the tag would not
    pin or identify its overlay source.
    """
    workflow = _read_workflow()
    # The tag branch must exist and hard-fail when the lock file is absent.
    assert 'elif [ "${is_tag}" = "tag" ]; then' in workflow, (
        "release-images.yml no longer distinguishes a tag push with a "
        "missing lock file"
    )
    refusal = "tagged release is missing .github/addons.lock.json"
    assert refusal in workflow, (
        "a tagged release without the lock file must be refused, not "
        "built from the mutable ADDON_REF branch"
    )
    # The refusal must fire (exit 1) BEFORE the mutable fallback branch is
    # reachable, so the tag can never slip through to the ADDON_REF path.
    refusal_idx = workflow.find(refusal)
    fallback_idx = workflow.find("using ADDON_REF=${ADDON_REF} (mutable)")
    assert 0 < refusal_idx < fallback_idx
    refusal_block = workflow[refusal_idx:workflow.find("else", refusal_idx)]
    assert "exit 1" in refusal_block


def test_release_workflow_fetches_locked_sha_detached():
    """The workflow must checkout each add-on detached from the locked
    SHA, not from a branch ref that a stray push could move.
    """
    workflow = _read_workflow()
    # Pin the exact checkout command used for locked SHAs: a detached
    # checkout of FETCH_HEAD, with the resolved SHA recorded.
    assert "git checkout --quiet --detach FETCH_HEAD" in workflow
    assert "addon-provenance.txt" in workflow


def test_release_workflow_records_resolved_sha_in_oci_labels():
    """The resolved SHA must be emitted as an OCI label so the published
    image is reproducible from a known commit.
    """
    workflow = _read_workflow()
    assert "org.opencontainers.image.source." in workflow
