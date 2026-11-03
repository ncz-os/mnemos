"""scripts/deploy_pythia.sh against fake podman/systemctl/curl binaries.

The fakes log every call. ``curl`` answers /health from the image currently
written in the fake quadlet, so a restart onto an image listed in
``FAKE_UNHEALTHY`` stays unhealthy and the script must roll back.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "deploy_pythia.sh"
OLD = "localhost/mnemos-enterprise:old"
NEW = "localhost/mnemos-enterprise:new"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")

_FAKE_PODMAN = r"""#!/usr/bin/env bash
echo "podman $*" >> "$FAKE_LOG"
case "$1 $2" in
  "image exists") case " $FAKE_IMAGES " in *" $3 "*) exit 0;; *) exit 1;; esac ;;
  "volume export") out=""; while [ $# -gt 0 ]; do [ "$1" = --output ] && out=$2; shift; done; echo data > "$out" ;;
esac
if [ "$1" = run ]; then exit "${FAKE_STYX_RC:-0}"; fi
exit 0
"""

_FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
echo "systemctl $*" >> "$FAKE_LOG"
if [ "$1" = restart ]; then
  n=$(( $(cat "$FAKE_LOG.restarts" 2>/dev/null || echo 0) + 1 )); echo $n > "$FAKE_LOG.restarts"
  touch "$FAKE_LOG.restarted"
  case " ${FAKE_RESTART_FAILS:-} " in *" $n "*) exit 1;; esac
fi
exit 0
"""

_FAKE_CURL = r"""#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_LOG"
img=$(sed -n 's/^Image=//p' "$MNEMOS_QUADLET")
[ -n "${FAKE_FAIL_AFTER_RESTART:-}" ] && [ -e "$FAKE_LOG.restarted" ] && exit 7
case " $FAKE_UNHEALTHY " in *" $img "*) exit 7;; esac
exit 0
"""


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("podman", _FAKE_PODMAN), ("systemctl", _FAKE_SYSTEMCTL), ("curl", _FAKE_CURL)):
        path = bindir / name
        path.write_text(body)
        path.chmod(0o755)
    quadlet = tmp_path / "mnemos-api.container"
    quadlet.write_text(f"[Container]\nImage={OLD}\nContainerName=mnemos-api\n")
    return {
        **os.environ,
        "PODMAN": str(bindir / "podman"),
        "SYSTEMCTL": str(bindir / "systemctl"),
        "CURL": str(bindir / "curl"),
        "MNEMOS_QUADLET": str(quadlet),
        "MNEMOS_DEPLOY_STATE": str(tmp_path / "state"),
        "MNEMOS_STYX_ENV": str(tmp_path / "styx.env"),
        "MNEMOS_HEALTH_INTERVAL": "0",
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "FAKE_IMAGES": f"{OLD} {NEW}",
        "FAKE_UNHEALTHY": "",
    }


def _run(env, *args, **overrides):
    result = subprocess.run(
        ["bash", str(SCRIPT), "--image", NEW, "--health-timeout", "0", *args],
        env={**env, **overrides},
        capture_output=True,
        text=True,
        timeout=60,
    )
    calls = Path(env["FAKE_LOG"]).read_text().splitlines() if Path(env["FAKE_LOG"]).exists() else []
    return result, calls


def _image(env):
    return Path(env["MNEMOS_QUADLET"]).read_text().split("Image=")[1].split("\n")[0]


def _mutations(calls):
    return [c for c in calls if not c.startswith(("podman image exists", "curl "))]


def test_dry_run_changes_nothing(env):
    result, calls = _run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _image(env) == OLD
    assert _mutations(calls) == []
    assert "would run: " in result.stdout and "dry run complete" in result.stdout
    assert not Path(env["MNEMOS_DEPLOY_STATE"]).exists()


def test_apply_requires_announcement(env):
    result, calls = _run(env, "--apply")
    assert result.returncode == 1
    assert "orchestrator.coordination" in result.stdout
    assert calls == []


def test_apply_backs_up_then_rolls_and_stays_on_healthy_new_image(env):
    result, calls = _run(env, "--apply", "--announced")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _image(env) == NEW
    mutations = _mutations(calls)
    assert mutations[0].startswith("podman volume export mnemos-api-data --output ")
    assert mutations[1].startswith(f"podman run --rm --network host --env-file {env['MNEMOS_STYX_ENV']} {NEW} ")
    assert mutations[2:] == ["systemctl daemon-reload", "systemctl restart mnemos-api.service"]
    history = (Path(env["MNEMOS_DEPLOY_STATE"]) / "history.log").read_text()
    assert f"prev={OLD} new={NEW}" in history


def test_unhealthy_new_image_rolls_back_to_previous(env):
    result, calls = _run(env, "--apply", "--announced", FAKE_UNHEALTHY=NEW)
    assert result.returncode == 3, result.stdout + result.stderr
    assert _image(env) == OLD
    assert _mutations(calls).count("systemctl restart mnemos-api.service") == 2
    assert "ROLLED-BACK" in (Path(env["MNEMOS_DEPLOY_STATE"]) / "history.log").read_text()


def test_failed_rollback_is_reported_distinctly(env):
    result, calls = _run(env, "--apply", "--announced", FAKE_FAIL_AFTER_RESTART="1")
    assert result.returncode == 4, result.stdout + result.stderr
    assert "ROLLBACK FAILED" in result.stdout
    assert _image(env) == OLD, "the quadlet still points at the previous image"
    assert _mutations(calls).count("systemctl restart mnemos-api.service") == 2


@pytest.mark.parametrize(("fails", "code"), [("1", 3), ("1 2", 4)])
def test_failed_restart_command_still_rolls_back(env, fails, code):
    """systemctl restart itself failing (not just an unhealthy start) must
    take the rollback path; a failing rollback restart reports exit 4."""
    result, calls = _run(env, "--apply", "--announced", FAKE_RESTART_FAILS=fails)
    assert result.returncode == code, result.stdout + result.stderr
    assert _image(env) == OLD
    assert _mutations(calls).count("systemctl restart mnemos-api.service") == 2
    assert "rolling out" in result.stdout
    if code == 4:
        assert "ROLLBACK FAILED" in result.stdout


def test_unhealthy_current_service_blocks_the_roll(env):
    result, calls = _run(env, "--apply", "--announced", FAKE_UNHEALTHY=OLD)
    assert result.returncode == 1
    assert "not healthy" in result.stdout
    assert _mutations(calls) == []


def test_styx_failure_blocks_the_roll_unless_explicitly_skipped(env):
    result, calls = _run(env, "--apply", "--announced", FAKE_STYX_RC="1")
    assert result.returncode == 2
    assert _image(env) == OLD
    assert not any(c.startswith("systemctl restart") for c in calls)

    Path(env["FAKE_LOG"]).unlink()
    result, calls = _run(
        env, "--apply", "--announced", "--skip-styx", "STYX is the fix being deployed", FAKE_STYX_RC="1"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _image(env) == NEW
    assert "proceeding on --skip-styx" in result.stdout


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"FAKE_IMAGES": OLD}, "image not in local storage"),
        ({"FAKE_IMAGES": NEW}, "rollback image missing"),
    ],
)
def test_preflight_refuses_missing_images(env, overrides, message):
    result, calls = _run(env, "--apply", "--announced", **overrides)
    assert result.returncode == 1
    assert message in result.stdout
    assert _mutations(calls) == []


def test_refuses_redeploying_the_current_image(env):
    result = subprocess.run(["bash", str(SCRIPT), "--image", OLD], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert "already the deployed image" in result.stdout


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_clean():
    result = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout
