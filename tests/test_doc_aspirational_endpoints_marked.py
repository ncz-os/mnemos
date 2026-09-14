"""Slice #198: pin the historical-note callouts on aspirational
endpoint references that are NOT live REST routes.

Surfaced by the deep documentation-sweep codex audit at HEAD
``de13b51`` (mem_1778221719446_2cdcad in MNEMOS):

- ``docs/DREAM_STATE_DESIGN.md`` describes
  ``/v1/dreams/{version_id}/promote``,
  ``/v1/dreams/{version_id}/acknowledge``, and
  ``/admin/dreams/run`` — none of those exist. The shipped
  MORPHEUS subsystem uses ``/v1/morpheus/runs*`` and
  ``/admin/morpheus/runs`` (`mnemos/api/routes/morpheus.py`).
The dream-state test does NOT remove the design content — it pins
that the historical-note callout stays near the aspirational
endpoint names so future readers don't grep for non-existent
routes without finding the explanation immediately.

The ``/admin/tunnels/*`` half of this file was INVERTED in v6.4.0.
Those endpoints, ``mnemos/tunnels/*`` and the helper script all
shipped, so the assertions below now pin the opposite property:
that the docs and the script do not regress into claiming the
feature is unimplemented, that the daemon code they promise is
really present, and that the remaining honest caveat (ephemeral
tunnels only; Cloudflare Named tunnels are still manual) survives.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_dream_endpoints_marked_did_not_ship():
    """`docs/DREAM_STATE_DESIGN.md` must keep the "did not ship
    as designed" callout near the section that introduces
    `/v1/dreams/*` and `/admin/dreams/run` endpoint names."""
    src = (REPO / "docs" / "DREAM_STATE_DESIGN.md").read_text()
    # Only enforce if the design names still appear (someone
    # could rewrite the section to drop them — that's also fine).
    if "/v1/dreams/" not in src and "/admin/dreams/" not in src:
        return
    assert "did not ship as designed" in src, (
        "docs/DREAM_STATE_DESIGN.md still references "
        "`/v1/dreams/*` or `/admin/dreams/run` but lost the "
        "historical-note callout. Add a note pointing readers at "
        "the live `mnemos/api/routes/morpheus.py` routes."
    )
    assert "/admin/morpheus/runs" in src, (
        "docs/DREAM_STATE_DESIGN.md historical-note callout no "
        "longer names the live `/admin/morpheus/runs` endpoint."
    )


def test_admin_tunnels_doc_no_longer_claims_the_feature_is_inert():
    """`/admin/tunnels/*` shipped in v6.4.0. The connector doc must not
    still tell operators the helper is inert — that would send them to
    the manual path for a feature that works."""
    src = (REPO / "docs" / "connectors"
           / "chatgpt-pro-developer-mode.md").read_text()
    stale = [
        phrase for phrase in (
            "currently inert",
            "neither has shipped",
            "aspirational contract",
        )
        if phrase in src
    ]
    assert not stale, (
        f"chatgpt-pro-developer-mode.md still carries pre-6.4.0 "
        f"not-implemented language {stale!r} for /admin/tunnels/*, which "
        f"shipped. Describe the working flow instead."
    )


def test_admin_tunnels_doc_states_the_host_opt_in():
    """The routes are gated behind MNEMOS_TUNNELS_ENABLED on top of root
    auth. An operator who doesn't know that reads the 403 as a broken
    token, so the doc has to name the flag."""
    src = (REPO / "docs" / "connectors"
           / "chatgpt-pro-developer-mode.md").read_text()
    assert "MNEMOS_TUNNELS_ENABLED" in src, (
        "chatgpt-pro-developer-mode.md documents the assisted tunnel path "
        "without naming the MNEMOS_TUNNELS_ENABLED host opt-in that gates it."
    )


def test_connectors_readme_no_longer_lists_tunnels_as_unimplemented():
    """`docs/connectors/README.md` stability commitments must not still
    list `/admin/tunnels/*` under 'not implemented'."""
    src = (REPO / "docs" / "connectors" / "README.md").read_text()
    assert "/admin/tunnels/*` are **not implemented**" not in src, (
        "docs/connectors/README.md still lists /admin/tunnels/* as not "
        "implemented; those routes shipped in v6.4.0."
    )
    # ...and must still be honest about what did NOT ship: named tunnels.
    assert "Named" in src, (
        "docs/connectors/README.md lost the caveat that only EPHEMERAL "
        "tunnels are managed and Cloudflare Named tunnels are still manual. "
        "Dropping it oversells the feature."
    )


def test_tunnel_script_is_no_longer_gated_behind_force():
    """The --force gate and 'currently inert' banner existed only because
    the daemon endpoints were missing. Both must be gone now that they
    aren't, or the helper stays unusable by default."""
    src = (REPO / "scripts" / "mnemos_tunnel_setup.py").read_text()
    assert 'if "--force" not in sys.argv' not in src, (
        "scripts/mnemos_tunnel_setup.py still refuses to run without "
        "--force. /admin/tunnels/* shipped in v6.4.0; drop the gate."
    )
    assert "currently inert" not in src, (
        "scripts/mnemos_tunnel_setup.py still prints the 'currently inert' "
        "warning for endpoints that now exist."
    )


def test_tunnel_daemon_modules_the_script_promises_actually_exist():
    """The counterpart to the old aspirational pin: the modules and routes
    the script's docstring names must be real, so this file keeps catching
    a docstring that promises more than the package ships."""
    tunnels = REPO / "mnemos" / "tunnels"
    for name in ("__init__.py", "base.py", "ngrok_bridge.py", "cloudflare_bridge.py"):
        assert (tunnels / name).is_file(), f"mnemos/tunnels/{name} is missing"

    routes = (REPO / "mnemos" / "api" / "routes" / "tunnels.py").read_text()
    for path in ('"/start"', '"/status"', '"/stop"'):
        assert path in routes, f"{path} route missing from mnemos/api/routes/tunnels.py"
    assert 'prefix="/admin/tunnels"' in routes, (
        "the tunnels router no longer mounts under /admin/tunnels; the "
        "connector docs and helper script both hardcode that prefix."
    )


def test_morpheus_routes_actually_exist():
    """Sanity-check: pin that the live morpheus routes named in
    the dream-state historical-note callout actually exist."""
    morpheus = (REPO / "mnemos" / "api" / "routes"
                / "morpheus.py").read_text()
    assert '"/admin/morpheus/runs"' in morpheus, (
        "/admin/morpheus/runs route disappeared from "
        "mnemos/api/routes/morpheus.py. Update the dream-state "
        "historical-note callout."
    )
    assert '"/v1/morpheus/runs"' in morpheus, (
        "/v1/morpheus/runs route disappeared. Update the "
        "dream-state historical-note callout."
    )
