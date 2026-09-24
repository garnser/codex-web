from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_web.api.ui import build_ui_router


class _UiService:
    def __init__(self) -> None:
        self.base_hrefs: list[str | None] = []
        self.event_hub = object()

    def index_html(self, *, base_href: str | None = None) -> str:
        self.base_hrefs.append(base_href)
        return f"<html><head></head><body data-base={base_href!r}>shell</body></html>"

    @staticmethod
    def devstatus_html(*, force_refresh: bool = False) -> str:
        return "devstatus"

    @staticmethod
    def devhealth_html(*, force_refresh: bool = False) -> str:
        return "devhealth"


def _client() -> tuple[TestClient, _UiService]:
    service = _UiService()
    app = FastAPI()
    app.include_router(build_ui_router(service))
    return TestClient(app), service


def test_project_landing_redirects_to_overview() -> None:
    client, _ = _client()

    response = client.get("/projects/home", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"].endswith("/projects/home/overview")


def test_supported_project_page_serves_operator_shell() -> None:
    client, service = _client()

    response = client.get("/projects/veridataops/automations")

    assert response.status_code == 200
    assert "shell" in response.text
    assert service.base_hrefs[-1] == ""


def test_configuration_project_page_serves_operator_shell() -> None:
    client, service = _client()

    response = client.get("/projects/veridataops/configuration")

    assert response.status_code == 200
    assert "shell" in response.text
    assert service.base_hrefs[-1] == ""


def test_unknown_project_page_is_not_a_shell_fallback() -> None:
    client, _ = _client()

    response = client.get("/projects/home/not-a-page")

    assert response.status_code == 404
