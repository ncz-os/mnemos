#!/usr/bin/env python3
"""Split-package installability matrix harness.

Verifies the carve promise: mnemos-core installs alone, and each add-on
(pantheon / knemon / graeae) can be ADDED INDIVIDUALLY on top of it, with
correct dependency closure and feature gating.

For each scenario it builds a fresh venv, installs a package subset from a
local wheelhouse (--find-links, no PyPI), and probes:
  * `pip check` (dependency consistency)
  * `is_extra_installed(...)` matches the expected gating for the subset
  * optional best-effort app boot + route enumeration (sqlite, no Oracle)

Adversarial dimensions (rotated by --round): install order permutations,
add-on-before-core, core upgrade after add-on, and a deliberate --no-deps
break case that MUST fail (proves deps are declared, not implied by co-install).

Exit 0 = all scenarios passed; 1 = at least one failure. JSON report to stdout.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

# extra -> the distribution that provides it (external add-ons only)
ADDON_DIST = {
    "pantheon": "mnemos-pantheon",
    "knemon": "mnemos-knemon",
    "graeae": "mnemos-graeae",
}
# pantheon depends on graeae + knemon (+ core); installing it must pull them.
ADDON_DEPS = {
    "pantheon": {"graeae", "knemon"},
    "knemon": set(),
    "graeae": set(),
}

PROBE = r"""
import json, sys
result = {"import_core": False, "extras": {}, "routes": None, "app_boot": None, "error": None}
try:
    import mnemos  # noqa
    from mnemos.core.extras import is_extra_installed
    result["import_core"] = True
    for x in ("pantheon", "knemon", "graeae", "nats", "persephone"):
        try:
            result["extras"][x] = bool(is_extra_installed(x))
        except Exception as e:  # noqa
            result["extras"][x] = "ERR:%s" % e
except Exception as e:  # noqa
    result["error"] = "core import: %r" % e
    print(json.dumps(result)); sys.exit(0)

# Best-effort app boot + route enumeration (sqlite, no external services).
# Use the OpenAPI schema paths (canonical) — app.routes contains included-router
# wrapper objects without a flat `.path`, so sub-router routes are invisible there.
try:
    from mnemos.api.main import app
    result["routes"] = sorted(app.openapi().get("paths", {}).keys())
    result["app_boot"] = True
except Exception as e:  # noqa
    result["app_boot"] = "ERR:%s" % type(e).__name__
print(json.dumps(result))
"""


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def make_venv(path: Path) -> Path:
    venv.EnvBuilder(with_pip=True).create(path)
    return path / "bin" / "python"


def pip_install(py: Path, wheelhouse: Path, dists: list[str], no_deps=False, extra_args=None):
    cmd = [str(py), "-m", "pip", "install", "--no-index", f"--find-links={wheelhouse}"]
    if no_deps:
        cmd.append("--no-deps")
    if extra_args:
        cmd += extra_args
    cmd += dists
    return run(cmd)


def probe(py: Path) -> dict:
    env = dict(os.environ)
    env.update(
        MNEMOS_DATABASE_URL="sqlite:///:memory:",
        MNEMOS_DB_PATH=":memory:",
        MNEMOS_DISABLE_NATS="1",
        MNEMOS_SKIP_MIGRATIONS="1",
        # Enable the graeae layer so /v1/providers mounts WHEN graeae is installed.
        # strict_layers stays unset (default False) so an enabled-but-absent layer
        # is skipped gracefully rather than failing boot. Do NOT enable hive: it is
        # not installable in this split and is configured to fail-fast when enabled.
        MNEMOS_ENABLE_GRAEAE="true",
    )
    r = run([str(py), "-c", PROBE], env=env)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {"error": "probe-unparseable", "stdout": r.stdout[-800:], "stderr": r.stderr[-800:]}


def expected_extras(installed_addons: set[str]) -> set[str]:
    """Closure: an add-on pulls its deps, so all transitively present are gated-on."""
    present = set(installed_addons)
    for a in list(installed_addons):
        present |= ADDON_DEPS.get(a, set())
    return present


def check_scenario(name, py, result, want_addons: set[str], expect_broken=False) -> dict:
    out = {"scenario": name, "passed": True, "checks": [], "probe": result}

    def chk(label, ok):
        out["checks"].append({label: bool(ok)})
        if not ok:
            out["passed"] = False

    if expect_broken:
        # Deliberate break: pantheon installed --no-deps WITHOUT its required
        # graeae/knemon. A PROPER install (with deps) would have pulled them, so an
        # inconsistent state — pantheon dist present but graeae absent — proves the
        # dependency is genuinely declared (not implied by co-install). The app must
        # still not have a working pantheon: graeae missing.
        extras = result.get("extras", {})
        broken = (extras.get("pantheon") is True) and (extras.get("graeae") is not True)
        chk("break_detected (pantheon present, graeae absent)", broken)
        return out

    chk("core_imports", result.get("import_core") is True)
    want = expected_extras(want_addons)
    extras = result.get("extras", {})
    for x in ("pantheon", "knemon", "graeae"):
        chk(f"extra_{x}=={x in want}", extras.get(x) is (x in want))
    # Route presence (only assert when app booted cleanly).
    routes = result.get("routes")
    if isinstance(routes, list):
        has_providers = any(p.startswith("/v1/providers") for p in routes)
        chk("providers_route==graeae", has_providers == ("graeae" in want))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wheelhouse", required=True)
    ap.add_argument("--round", type=int, default=0, help="rotates adversarial order/cases")
    ap.add_argument("--keep", action="store_true", help="keep venvs for debugging")
    args = ap.parse_args()
    wh = Path(args.wheelhouse).resolve()
    assert wh.is_dir(), f"wheelhouse not found: {wh}"

    work = Path(tempfile.mkdtemp(prefix="mnemos-matrix-"))
    report = {"round": args.round, "wheelhouse": str(wh), "scenarios": []}

    # Base scenarios: each add-on individually + combinations.
    base = [
        ("core-only", set()),
        ("core+knemon", {"knemon"}),
        ("core+graeae", {"graeae"}),
        ("core+pantheon", {"pantheon"}),  # must pull graeae+knemon
        ("core+graeae+knemon", {"graeae", "knemon"}),
        ("full", {"pantheon", "knemon", "graeae"}),
    ]
    # Adversarial install ORDER for this round (permute add-on install sequence).
    perms = list(itertools.permutations(["mnemos-graeae", "mnemos-knemon", "mnemos-pantheon"]))
    order = perms[args.round % len(perms)]

    for name, addons in base:
        vp = work / name.replace("+", "_")
        py = make_venv(vp)
        dists = ["mnemos-core"] + [ADDON_DIST[a] for a in addons]
        # Round-rotated: install order varies; sometimes add-on first then core.
        if args.round % 2 == 1 and addons:
            ordered = [d for d in order if d in dists] + ["mnemos-core"]
            inst = pip_install(py, wh, ordered)
        else:
            inst = pip_install(py, wh, dists)
        if inst.returncode != 0:
            report["scenarios"].append(
                {"scenario": name, "passed": False, "checks": [{"pip_install": False}], "stderr": inst.stderr[-1200:]}
            )
            if not args.keep:
                shutil.rmtree(vp, ignore_errors=True)
            continue
        pc = run([str(py), "-m", "pip", "check"])
        res = probe(py)
        sc = check_scenario(name, py, res, addons)
        sc["pip_check_ok"] = pc.returncode == 0
        if pc.returncode != 0:
            sc["passed"] = False
            sc["pip_check"] = pc.stdout[-600:]
        report["scenarios"].append(sc)
        if not args.keep:
            shutil.rmtree(vp, ignore_errors=True)

    # Deliberate break case: install pantheon --no-deps WITHOUT core's add-ons,
    # proving deps are declared (not implicitly co-installed).
    vp = work / "break-pantheon-nodeps"
    py = make_venv(vp)
    pip_install(py, wh, ["mnemos-core"])
    pip_install(py, wh, ["mnemos-pantheon"], no_deps=True)
    res = probe(py)
    report["scenarios"].append(check_scenario("break-pantheon-nodeps", py, res, {"pantheon"}, expect_broken=True))
    if not args.keep:
        shutil.rmtree(vp, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)

    report["all_passed"] = all(s.get("passed") for s in report["scenarios"])
    print(json.dumps(report, indent=2))
    sys.exit(0 if report["all_passed"] else 1)


if __name__ == "__main__":
    main()
