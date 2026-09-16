from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, FastAPI


def _filter_routes(routes: list[Any], paths: set[str]) -> tuple[list[Any], int]:
    kept: list[Any] = []
    removed = 0
    for route in routes:
        nested = getattr(route, "routes", None)
        if isinstance(nested, list):
            filtered, nested_removed = _filter_routes(list(nested), paths)
            nested[:] = filtered
            removed += nested_removed
        if getattr(route, "path", None) in paths:
            removed += 1
            continue
        kept.append(route)
    return kept, removed


def replace_routes(
    app: FastAPI,
    router: APIRouter,
    *,
    paths: Iterable[str],
    key: str,
) -> int:
    """Install one domain router after removing any legacy handlers.

    The existing FastAPI instance is intentionally preserved because runtime
    lifecycle hooks and long-lived Codex state are still being migrated out of
    the legacy runtime. A domain may already have no legacy handlers left; in
    that case the router is still installed normally.

    The return value is the number of routes owned by the installed domain
    router, not the number of legacy routes removed. This keeps composition
    diagnostics stable as obsolete handlers are physically deleted.
    """

    marker = f"domain_router_{key}_installed"
    if getattr(app.state, marker, False):
        return 0

    path_set = set(paths)
    filtered, removed = _filter_routes(list(app.router.routes), path_set)
    app.router.routes[:] = filtered
    app.include_router(router)
    app.openapi_schema = None
    setattr(app.state, marker, True)
    setattr(app.state, f"domain_router_{key}_legacy_removed", removed)
    return len(router.routes)
