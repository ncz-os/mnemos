"""Route introspection that sees through FastAPI's included-router wrappers."""

from __future__ import annotations

from typing import Any, Iterator


class _FlatRoute:
    """A route seen through its include prefixes: ``path`` is the full path."""

    def __init__(self, route: Any, prefix: str) -> None:
        self._route = route
        self.path = prefix + route.path

    def __getattr__(self, name: str) -> Any:
        return getattr(self._route, name)


def flatten_routes(routes: Any, prefix: str = "") -> Iterator[Any]:
    """Yield every concrete route with its include prefixes applied.

    Newer FastAPI versions wrap included routers in objects that carry the
    original router and the include context instead of exposing the routes
    directly on ``app.routes``.
    """
    for route in routes:
        if hasattr(route, "original_router") and hasattr(route, "include_context"):
            inner_prefix = prefix + (route.include_context.prefix or "")
            yield from flatten_routes(route.original_router.routes, inner_prefix)
        elif hasattr(route, "path"):
            yield _FlatRoute(route, prefix) if prefix else route
