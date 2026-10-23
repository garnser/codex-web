from __future__ import annotations

import unittest

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


class DefinitionProjectRouteTests(unittest.TestCase):
    def test_profile_management_destination_serves_shell_at_both_mounts(self) -> None:
        for prefix in ("", "/codex"):
            with self.subTest(prefix=prefix):
                service = _UiService()
                app = FastAPI()
                app.include_router(build_ui_router(service))
                with TestClient(app, root_path=prefix) as client:
                    response = client.get("/projects/project-a/definitions")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(service.base_hrefs[-1], prefix)


class ProjectWorkspaceRouteTests(unittest.TestCase):
    PAGES = (
        'overview', 'work-items', 'runs', 'chat', 'agents', 'agent-profiles',
        'teams', 'automations', 'attention', 'operations', 'project-settings',
        'configuration', 'secrets', 'definitions', 'goals', 'decisions', 'metrics',
        'company', 'memory', 'skills', 'integrations', 'workers', 'resources',
        'organization',
    )

    def test_every_workspace_destination_serves_shell_at_both_mounts(self) -> None:
        for prefix in ('', '/codex'):
            service = _UiService()
            app = FastAPI()
            app.include_router(build_ui_router(service))
            parent = FastAPI()
            parent.mount(prefix or '/', app)
            with TestClient(parent) as client:
                for page in self.PAGES:
                    with self.subTest(prefix=prefix, page=page):
                        response = client.get(f'{prefix}/projects/project-a/{page}')
                        self.assertEqual(response.status_code, 200)
                        self.assertIn('shell', response.text)
                        self.assertEqual(service.base_hrefs[-1], prefix)
                with self.subTest(prefix=prefix, page='invalid'):
                    self.assertEqual(client.get(f'{prefix}/projects/project-a/not-a-page').status_code, 404)
                response = client.get(f'{prefix}/projects/project-a', follow_redirects=False)
                self.assertEqual(response.status_code, 307)
                self.assertEqual(response.headers['location'], f'http://testserver{prefix}/projects/project-a/overview')


class ProjectLandingQueryTests(unittest.TestCase):
    """Exercise the actual redirect, rather than only checking shortcut markup."""

    def client(self, prefix: str):
        service = _UiService()
        app = FastAPI()
        app.include_router(build_ui_router(service))
        parent = FastAPI()
        parent.mount(prefix or '/', app)
        return TestClient(parent), service

    def test_existing_conversation_survives_project_landing_at_both_mounts(self):
        from urllib.parse import urlsplit, parse_qs
        for prefix in ('', '/codex'):
            with self.subTest(prefix=prefix), self.client(prefix)[0] as client:
                response = client.get(
                    f'{prefix}/projects/home?thread=same-existing-thread',
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 307)
                target = urlsplit(response.headers['location'])
                self.assertEqual(target.path, f'{prefix}/projects/home/overview')
                self.assertEqual(parse_qs(target.query), {'thread': ['same-existing-thread']})
                landed = client.get(response.headers['location'])
                self.assertEqual(landed.status_code, 200)
                self.assertEqual(landed.url.params['thread'], 'same-existing-thread')

    def test_encoded_and_duplicate_query_values_remain_data_on_same_origin(self):
        from urllib.parse import urlsplit, parse_qsl
        query = 'thread=existing%2Fid%3Fvalue%3Dx&filter=one&filter=two&return_to=https%3A%2F%2Fother.invalid%2Fx%3Fa%3Db&empty='
        with self.client('/codex')[0] as client:
            response = client.get('/codex/projects/home?' + query, follow_redirects=False)
            target = urlsplit(response.headers['location'])
            self.assertEqual((target.scheme, target.netloc), ('http', 'testserver'))
            self.assertEqual(target.path, '/codex/projects/home/overview')
            self.assertEqual(target.query, query)
            self.assertEqual(parse_qsl(target.query, keep_blank_values=True),
                             parse_qsl(query, keep_blank_values=True))

    def test_ordinary_project_without_query_keeps_canonical_destination(self):
        for prefix in ('', '/codex'):
            with self.subTest(prefix=prefix), self.client(prefix)[0] as client:
                response = client.get(f'{prefix}/projects/project-a', follow_redirects=False)
                self.assertEqual(response.status_code, 307)
                self.assertEqual(response.headers['location'],
                                 f'http://testserver{prefix}/projects/project-a/overview')
