"""Optional-extra HTTP guards.

Mirror of the PANTHEON ``_require_enabled`` pattern as a reusable FastAPI
dependency so add-on routers (KNEMON, etc.) return a clean ``503`` with the
standard missing-extra detail when the extra is absent/disabled, instead of
crashing. Attach at router level: ``APIRouter(dependencies=[require_extra("knemon")])``.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException

from mnemos.core.extras import is_extra_installed, missing_extra_detail


def require_extra(name: str, *, label: str | None = None):
    """Return a FastAPI dependency that 503s when optional extra ``name`` is absent."""

    def _dep() -> None:
        if not is_extra_installed(name):
            raise HTTPException(status_code=503, detail=missing_extra_detail(name, label=label))

    return Depends(_dep)
