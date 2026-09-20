from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.identity import TenantScope
from codex_web.models import BotBinding, Project, ThreadRunSettings
from codex_web.resources import (
    Resource,
    ResourceLifecycle,
    ResourceType,
)
from codex_web.services.project_ui_state import ProjectUiStateService
from codex_web.services.projects import ProjectNotFoundError


class _Projects:
    def __init__(self) -> None:
        self.project = Project(
            id="project-a",
            organization_id="org-a",
            workspace_id="workspace-a",
            name="Project A",
            path="/workspace/project-a",
        )

    def get(self, project_id: str, scope: TenantScope):
        if (
            project_id != self.project.id
            or scope.organization_id != self.project.organization_id
            or scope.workspace_id != self.project.workspace_id
        ):
            raise ProjectNotFoundError("Project not found")
        return self.project


class _Resources:
    def __init__(self, count: int = 250) -> None:
        self.values = [
            Resource(
                id=f"repository-{index:04d}",
                organization_id="org-a",
                workspace_id="workspace-a",
                resource_type=ResourceType.REPOSITORY,
                lifecycle=ResourceLifecycle.ACTIVE,
                name=f"Repository {index:04d}",
            )
            for index in range(count)
        ]
        self.values.extend(
            Resource(
                id=f"service-{index:04d}",
                organization_id="org-a",
                workspace_id="workspace-a",
                resource_type=ResourceType.SERVICE,
                lifecycle=ResourceLifecycle.ACTIVE,
                name=f"Service {index:04d}",
            )
            for index in range(50)
        )
        self.last_actor = None

    def project_resources(self, project, *, actor):
        self.last_actor = actor
        return list(self.values)


class _Threads:
    def __init__(self, count: int = 100) -> None:
        self.count = count
        self.revision = 7.0
        self.calls: list[dict] = []

    async def list(
        self,
        project_id,
        archived=False,
        search=None,
        *,
        limit=None,
        cursor=None,
    ):
        self.calls.append(
            {
                "project_id": project_id,
                "archived": archived,
                "search": search,
                "limit": limit,
                "cursor": cursor,
            }
        )
        size = min(int(limit or 50), self.count)
        return {
            "data": [
                {
                    "id": f"thread-{index:04d}",
                    "name": f"Thread {index:04d}",
                    "updatedAt": float(index),
                    "status": {"type": "notLoaded"},
                }
                for index in range(size)
            ],
            "pageSize": size,
            "nextCursor": "next-page" if self.count > size else None,
            "hasMore": self.count > size,
            "revision": self.revision,
        }


class _Bindings:
    def __init__(self, count: int = 700) -> None:
        self.unrelated_installation_bindings = 100_000
        self.scoped_calls = 0
        self.global_calls = 0
        self.values = [
            BotBinding(
                id=f"binding-{index:04d}",
                provider="slack",
                external_conversation_id=f"C{index:06d}",
                thread_id="thread-0000",
                project_id="project-a",
                external_name=f"channel-{index:04d}",
                created_at=float(index),
                updated_at=float(index),
            )
            for index in range(count)
        ]

    def for_project_all(self, project_id: str):
        self.scoped_calls += 1
        if project_id != "project-a":
            return []
        return list(self.values)

    def load_bindings(self):
        self.global_calls += 1
        raise AssertionError(
            "Project UI state must not scan the global binding registry"
        )


class _Settings:
    def __init__(self) -> None:
        self.requested: list[str] = []

    def get(self, thread_id: str):
        self.requested.append(thread_id)
        return ThreadRunSettings(
            sandbox="workspace-write",
            approval_policy="on-request",
        )


class _Channels:
    def __init__(self, count: int = 400) -> None:
        self.values = [
            {
                "provider": "slack",
                "id": f"C{index:06d}",
                "name": f"channel-{index:04d}",
                "label": f"#channel-{index:04d}",
            }
            for index in range(count)
        ]

    async def list(self, project_id: str):
        if project_id != "project-a":
            return []
        return [dict(item) for item in self.values]

    def status(self, project_id: str | None = None):
        return {
            "projectId": project_id,
            "cacheHit": True,
        }


class _Profiles:
    def __init__(self) -> None:
        self.calls = 0

    def public(self, *, project_id: str | None = None):
        self.calls += 1
        return {
            "items": [
                {
                    "id": "repository-write",
                    "name": "Repository write",
                }
            ],
            "default_profile_id": "repository-write",
            "project_id": project_id,
        }


def _actor(
    organization_id: str = "org-a",
    workspace_id: str = "workspace-a",
):
    return SimpleNamespace(
        tenant=TenantScope(
            organization_id=organization_id,
            workspace_id=workspace_id,
        ),
        organization_id=organization_id,
        workspace_id=workspace_id,
    )


def _binding_public(binding: BotBinding) -> dict:
    return binding.model_dump(mode="json")


class ProjectUiStateTests(unittest.IsolatedAsyncioTestCase):
    def _service(self):
        projects = _Projects()
        resources = _Resources()
        threads = _Threads()
        bindings = _Bindings()
        settings = _Settings()
        channels = _Channels()
        profiles = _Profiles()
        service = ProjectUiStateService(
            projects=projects,
            resources=resources,
            threads=threads,
            bindings=bindings,
            settings=settings,
            channels=channels,
            execution_profiles=profiles,
            binding_public=_binding_public,
        )
        return (
            service,
            resources,
            threads,
            bindings,
            settings,
            channels,
            profiles,
        )

    async def test_projection_is_project_scoped_and_bounded(self) -> None:
        (
            service,
            resources,
            threads,
            bindings,
            settings,
            _channels,
            profiles,
        ) = self._service()

        payload = await service.state(
            "project-a",
            actor=_actor(),
            thread_limit=1000,
        )

        self.assertEqual(threads.calls[0]["limit"], 100)
        self.assertEqual(len(payload["threads"]["data"]), 100)
        self.assertEqual(len(payload["resources"]["items"]), 200)
        self.assertTrue(payload["resources"]["truncated"])
        self.assertEqual(len(payload["bindings"]["items"]), 500)
        self.assertTrue(payload["bindings"]["truncated"])
        self.assertEqual(len(payload["channels"]["items"]), 250)
        self.assertTrue(payload["channels"]["truncated"])
        self.assertEqual(len(payload["threadSettings"]), 100)
        self.assertEqual(len(settings.requested), 100)
        self.assertEqual(bindings.scoped_calls, 1)
        self.assertEqual(bindings.global_calls, 0)
        self.assertEqual(
            bindings.unrelated_installation_bindings,
            100_000,
        )
        self.assertEqual(profiles.calls, 1)
        self.assertIs(resources.last_actor.organization_id, "org-a")
        self.assertEqual(payload["meta"]["contractVersion"], 1)
        self.assertIn("bindings", payload["meta"]["sectionVersions"])
        self.assertIn("threads", payload["meta"]["sectionVersions"])

    async def test_cross_tenant_project_lookup_fails_closed(self) -> None:
        service, *_rest = self._service()

        with self.assertRaises(ProjectNotFoundError):
            await service.state(
                "project-a",
                actor=_actor("org-b", "workspace-b"),
            )

    async def test_static_sections_can_be_omitted_after_client_cache(self) -> None:
        service, *_middle, profiles = self._service()

        first = await service.state(
            "project-a",
            actor=_actor(),
            include_static=True,
        )
        second = await service.state(
            "project-a",
            actor=_actor(),
            include_static=False,
        )

        self.assertIsNotNone(first["project"])
        self.assertIsNotNone(first["executionProfiles"])
        self.assertIsNone(second["project"])
        self.assertIsNone(second["executionProfiles"])
        self.assertEqual(profiles.calls, 1)
        self.assertFalse(second["meta"]["staticIncluded"])
        self.assertNotIn(
            "executionProfiles",
            second["meta"]["sectionVersions"],
        )

    async def test_etag_and_section_versions_change_with_thread_revision(self) -> None:
        service, _resources, threads, *_rest = self._service()

        first = await service.state(
            "project-a",
            actor=_actor(),
        )
        repeated = await service.state(
            "project-a",
            actor=_actor(),
        )
        self.assertEqual(
            service.etag(first),
            service.etag(repeated),
        )
        self.assertEqual(
            first["meta"]["sectionVersions"],
            repeated["meta"]["sectionVersions"],
        )

        threads.revision = 8.0
        changed = await service.state(
            "project-a",
            actor=_actor(),
        )
        self.assertNotEqual(
            service.etag(first),
            service.etag(changed),
        )
        self.assertNotEqual(
            first["meta"]["sectionVersions"]["threads"],
            changed["meta"]["sectionVersions"]["threads"],
        )


if __name__ == "__main__":
    unittest.main()
