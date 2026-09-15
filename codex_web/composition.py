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
    """Replace legacy route handlers with one domain router, idempotently.

    The existing FastAPI instance is intentionally preserved because runtime
    lifecycle hooks and long-lived Codex state are still being migrated out of
    the legacy runtime. Extracted domains, however, are removed from that
    route table and re-registered from their owning modules.
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
    return removed
