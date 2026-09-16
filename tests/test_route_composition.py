from __future__ import annotations

import unittest

from fastapi import APIRouter, FastAPI

from codex_web.composition import replace_routes


class RouteCompositionTests(unittest.TestCase):
    def test_router_installs_when_no_legacy_handler_exists(self) -> None:
        app = FastAPI()
        router = APIRouter()

        @router.get("/api/example")
        async def example() -> dict[str, bool]:
            return {"ok": True}

        installed = replace_routes(
            app,
            router,
            paths={"/api/example"},
            key="example",
        )

        self.assertEqual(installed, 1)
        self.assertEqual(app.state.domain_router_example_legacy_removed, 0)
        self.assertEqual(
            [route.path for route in app.router.routes if getattr(route, "path", None) == "/api/example"],
            ["/api/example"],
        )

    def test_router_replaces_legacy_handler_and_reports_installed_count(self) -> None:
        app = FastAPI()

        @app.get("/api/example")
        async def legacy() -> dict[str, str]:
            return {"owner": "legacy"}

        router = APIRouter()

        @router.get("/api/example")
        async def extracted() -> dict[str, str]:
            return {"owner": "domain"}

        installed = replace_routes(
            app,
            router,
            paths={"/api/example"},
            key="example",
        )

        self.assertEqual(installed, 1)
        self.assertEqual(app.state.domain_router_example_legacy_removed, 1)
        matching = [route for route in app.router.routes if getattr(route, "path", None) == "/api/example"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].endpoint.__name__, "extracted")


if __name__ == "__main__":
    unittest.main()
