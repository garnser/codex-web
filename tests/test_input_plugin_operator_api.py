from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.input_plugins import build_input_plugins_router
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.input_plugin_definitions import InputPipelineDefinition


class _Catalog:
    @staticmethod
    def metadata():
        return (
            {
                "plugin_id": "builtin.normalize-whitespace",
                "plugin_version": "1.0.0",
                "transport": "builtin",
            },
            {
                "plugin_id": "prompt-master",
                "plugin_version": "1.8.0",
                "transport": "skill",
            },
        )


class _Service:
    catalog = _Catalog()

    @staticmethod
    def record(**_kwargs):
        payload = InputPipelineDefinition.model_validate(
            {
                "registrations": [
                    {
                        "plugin_id": "builtin.normalize-whitespace",
                        "plugin_version": "1.0.0",
                        "phase": "normalize",
                        "order": 10,
                        "enabled": True,
                        "failure_policy": "fail_closed",
                        "max_patch_bytes": 4096,
                        "max_added_characters": 0,
                        "conditions": {},
                        "settings": {"trim": True},
                    },
                    {
                        "plugin_id": "prompt-master",
                        "plugin_version": "1.9.0",
                        "phase": "compose",
                        "order": 20,
                        "enabled": True,
                        "failure_policy": "skip",
                        "max_patch_bytes": 8192,
                        "max_added_characters": 4000,
                        "conditions": {
                            "purposes": ["coding"],
                            "model_classes": ["primary-coding"],
                        },
                        "settings": {"strategy": "agentic-coding"},
                    },
                ]
            }
        ).model_dump(mode="json")
        return SimpleNamespace(
            definition_id="input-pipeline.default",
            kind="input_pipeline",
            record_id="definition-record-1",
            revision=7,
            definition_schema_version="1.0",
            scope_type=SimpleNamespace(value="workspace"),
            scope_id="ws-a",
            lifecycle=SimpleNamespace(value="published"),
            checksum="a" * 64,
            published_by="publisher",
            published_at=1234.0,
            effective_from=None,
            effective_until=None,
            payload=payload,
        )


class InputPluginOperatorApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="operator",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(build_input_plugins_router(_Service()))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

    def test_admin_sees_effective_definition_and_local_catalog_availability(self) -> None:
        response = self.client.get("/api/input-plugins")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["definition"]["record_id"], "definition-record-1")
        self.assertEqual(payload["definition"]["revision"], 7)
        self.assertEqual(payload["definition"]["scope_type"], "workspace")

        configured = payload["registrations"]
        self.assertEqual(len(configured), 2)
        self.assertTrue(configured[0]["implementation_available"])
        self.assertEqual(configured[0]["implementation_transport"], "builtin")
        self.assertEqual(configured[0]["settings"], {"trim": True})

        self.assertEqual(configured[1]["plugin_version"], "1.9.0")
        self.assertFalse(configured[1]["implementation_available"])
        self.assertIsNone(configured[1]["implementation_transport"])
        self.assertEqual(configured[1]["settings"], {"strategy": "agentic-coding"})

        catalog = {
            (item["plugin_id"], item["plugin_version"]): item["transport"]
            for item in payload["catalog"]
        }
        self.assertEqual(catalog[("prompt-master", "1.8.0")], "skill")

        serialized = response.text.casefold()
        self.assertNotIn("skill.md", serialized)
        self.assertNotIn("system_prompt", serialized)
        self.assertNotIn("context_blocks", serialized)

    def test_ordinary_member_cannot_read_operator_projection(self) -> None:
        self.actor = self.actor.model_copy(
            update={"roles": (MembershipRole.MEMBER,)}
        )

        response = self.client.get("/api/input-plugins")

        self.assertEqual(response.status_code, 403)
        self.assertIn("administrator", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
