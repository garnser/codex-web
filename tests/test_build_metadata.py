from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_web.api.runtime import build_runtime_router
from codex_web.api.system import build_system_router


class _StaticAssets:
    @staticmethod
    def version() -> str:
        return "static-test"


class _RuntimeHealth:
    @staticmethod
    def health():
        return {"ok": True}


class _Diagnostics:
    @staticmethod
    def project_lookup(_project_id):
        raise KeyError("unused")


class _Routing:
    @staticmethod
    def preview(_payload):
        return {}


class BuildMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(
            build_system_router(
                _StaticAssets(),
                _RuntimeHealth(),
                _Diagnostics(),
                _Routing(),
            )
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

    def test_version_endpoint_reports_release_and_exact_source_revision(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_RELEASE_VERSION": "0.2.0",
                "CODEX_WEB_GIT_REVISION": "abc123def456",
                "CODEX_WEB_BUILD_SOURCE": (
                    "https://github.com/garnser/codex-web"
                ),
            },
            clear=False,
        ):
            response = self.client.get("/api/version")
            live = self.client.get("/api/livez")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "staticVersion": "static-test",
                "releaseVersion": "0.2.0",
                "gitRevision": "abc123def456",
                "source": "https://github.com/garnser/codex-web",
            },
        )
        self.assertEqual(
            live.json()["build"]["gitRevision"],
            "abc123def456",
        )
        self.assertEqual(
            live.json()["build"]["releaseVersion"],
            "0.2.0",
        )

    def test_livez_has_one_canonical_owner_when_runtime_router_is_composed_first(self) -> None:
        app = FastAPI()
        app.include_router(build_runtime_router(SimpleNamespace()))
        app.include_router(
            build_system_router(
                _StaticAssets(),
                _RuntimeHealth(),
                _Diagnostics(),
                _Routing(),
            )
        )

        live_routes = [
            route
            for route in app.routes
            if getattr(route, "path", None) == "/api/livez"
        ]
        self.assertEqual(len(live_routes), 1)

        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_RELEASE_VERSION": "0.2.0",
                "CODEX_WEB_GIT_REVISION": "abc123def456",
            },
            clear=False,
        ):
            with TestClient(app) as client:
                response = client.get("/api/livez")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["build"]["releaseVersion"],
            "0.2.0",
        )
        self.assertEqual(
            response.json()["build"]["gitRevision"],
            "abc123def456",
        )

    def test_build_metadata_defaults_are_explicit_not_inferred_from_mounts(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CODEX_WEB_RELEASE_VERSION": "",
                "CODEX_WEB_GIT_REVISION": "",
                "CODEX_WEB_BUILD_SOURCE": "",
            },
            clear=False,
        ):
            response = self.client.get("/api/version")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["releaseVersion"], "dev")
        self.assertEqual(response.json()["gitRevision"], "unknown")
        self.assertEqual(
            response.json()["source"],
            "https://github.com/garnser/codex-web",
        )


if __name__ == "__main__":
    unittest.main()
