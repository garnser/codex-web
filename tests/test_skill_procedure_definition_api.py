"""Real generic Definition HTTP routes, including schema pre-registration order."""
from __future__ import annotations

import unittest

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.definitions import build_definitions_router
from codex_web.api.skills import build_skills_router
from codex_web.definitions import DefinitionLifecycle, reference_for
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.skills import install_skill_definition_schema
from codex_web.skill_security import SkillScanRequest, SkillSecurityPolicyUpdate
from codex_web.skills import SkillDefinition
from codex_web.storage.definition_registry import DefinitionRegistryStore
from tests import test_skill_procedures as fixtures

procedure = fixtures.procedure


class ProcedureDefinitionApiTests(unittest.TestCase):
    pre_registered = True
    draft = fixtures.ProcedureTests.draft
    publish = fixtures.ProcedureTests.publish

    def setUp(self):
        fixtures.ProcedureTests.setUp(self)
        self.actor = self.admin
        self.app = FastAPI()

        @self.app.middleware("http")
        async def actor_middleware(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        self.app.include_router(build_definitions_router(self.definitions))
        self.app.include_router(build_skills_router(self.skills))
        # Raise unexpected exceptions so an unhandled server failure fails the
        # test instead of being confused with an expected typed denial.
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        fixtures.ProcedureTests.tearDown(self)

    def request_payload(self, content=None):
        return {
            "definition_id": "procedure-http", "kind": "agent.skill", "definition_schema_version": "1.0",
            "scope_type": "workspace", "scope_id": "workspace-a",
            "payload": SkillDefinition(name="HTTP procedure", instructions="Follow canonical policy", owner_identity_id="admin",
                                       operational_procedure=content or procedure()).model_dump(mode="json"),
        }

    def test_unsupported_reference_returns_typed_denial_and_creates_no_record(self):
        content = procedure(); content["steps"][0]["action"]["action_id"] = "host.shell"
        before = self.definitions.store.load()
        result = self.client.post("/api/definitions/drafts", json=self.request_payload(content))
        self.assertEqual(result.status_code, 409, result.text)
        denial = result.json()["detail"]
        self.assertEqual(denial["code"], "procedure_reference_invalid")
        self.assertEqual(denial["gate"], "references")
        self.assertEqual(denial["definitionReference"]["definition_schema_version"], "1.0")
        self.assertEqual(len(denial["definitionReference"]["checksum"]), 64)
        self.assertEqual(self.definitions.store.load(), before)

    def test_pre_registration_does_not_bypass_validate_metadata_gate(self):
        response = self.client.post("/api/definitions/drafts", json=self.request_payload())
        self.assertEqual(response.status_code, 200, response.text)
        record = response.json()["record"]
        self.actions.unregister_tenant("ops", "test", tenant_scope=self.admin.tenant)
        result = self.client.post(f"/api/definitions/{record['record_id']}/validate", json={})
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()["detail"]["code"], "procedure_reference_invalid")
        after = self.definitions.get_record(record["record_id"])
        self.assertEqual(after.lifecycle, DefinitionLifecycle.DRAFT)
        self.assertEqual(after.checksum, record["checksum"])

    def test_unscanned_publication_preserves_exact_draft_and_policy_evidence(self):
        policy = self.security.update_policy(SkillSecurityPolicyUpdate(scan_locally_authored=True, reason="Require reviewed procedure scans"), actor=self.admin)
        created = self.client.post("/api/definitions/drafts", json=self.request_payload())
        self.assertEqual(created.status_code, 200, created.text)
        record = created.json()["record"]
        result = self.client.post(f"/api/definitions/{record['record_id']}/publish", json={})
        self.assertEqual(result.status_code, 409, result.text)
        denial = result.json()["detail"]
        self.assertEqual(denial["code"], "procedure_security_blocked")
        self.assertEqual(denial["definitionReference"]["checksum"], record["checksum"])
        self.assertEqual(denial["definitionReference"]["record_id"], record["record_id"])
        evidence = denial["evidence"]
        self.assertEqual(evidence["policyReference"]["record_id"], policy["definitionReference"]["record_id"])
        self.assertEqual(evidence["policyReference"]["checksum"], policy["definitionReference"]["checksum"])
        self.assertEqual(evidence["security"]["status"], "not_scanned")
        after = self.definitions.get_record(record["record_id"])
        self.assertEqual(after.lifecycle, DefinitionLifecycle.DRAFT)
        self.assertEqual(after.payload, record["payload"])
        self.assertIsNone(after.published_at)

    def test_expired_publication_preserves_scan_and_draft(self):
        view = self.draft(imported=True)
        self.security.scan(view, SkillScanRequest(), actor=self.admin)
        report = self.security.report(view, actor=self.admin)
        self.now = 200
        result = self.client.post(f"/api/definitions/{view['recordId']}/publish", json={})
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()["detail"]["code"], "procedure_expired")
        self.assertEqual(result.json()["detail"]["definitionReference"], view["definitionReference"])
        self.assertEqual(self.security.report(view, actor=self.admin), report)
        self.assertEqual(self.definitions.get_record(view["recordId"]).lifecycle, DefinitionLifecycle.DRAFT)

    def test_unscanned_rollback_returns_typed_denial_retains_draft_and_history(self):
        view = self.draft(imported=True)
        self.security.scan(view, SkillScanRequest(), actor=self.admin)
        self.publish(view)
        original = self.definitions.get_record(view["recordId"])
        scan_evidence = self.security.report(view, actor=self.admin)
        result = self.client.post("/api/definitions/rollback", json={
            "definition_id": view["skillId"], "kind":"agent.skill", "scope_type":"workspace",
            "scope_id":"workspace-a", "target_revision":view["revision"], "reason":"Reviewed rollback",
        })
        self.assertEqual(result.status_code, 409, result.text)
        denial = result.json()["detail"]
        self.assertEqual(denial["code"], "procedure_security_blocked")
        draft = self.definitions.get_record(denial["definitionReference"]["record_id"])
        self.assertEqual(draft.lifecycle, DefinitionLifecycle.DRAFT)
        self.assertEqual(draft.rollback_of_record_id, original.record_id)
        self.assertGreater(draft.revision, original.revision)
        self.assertEqual(draft.payload, original.payload)
        self.assertEqual(self.definitions.get_record(original.record_id), original)
        self.assertEqual(self.security.report(view, actor=self.admin), scan_evidence)
        new_view = self.skills.get(view["skillId"], actor=self.admin)
        self.security.scan(new_view, SkillScanRequest(), actor=self.admin)
        approved = self.client.post(f"/api/definitions/{draft.record_id}/publish", json={})
        self.assertEqual(approved.status_code, 200, approved.text)

    def test_expired_rollback_returns_typed_denial_retains_new_inactive_revision(self):
        view = self.publish(self.draft())
        original = self.definitions.get_record(view["recordId"])
        self.now = 200
        result = self.client.post("/api/definitions/rollback", json={
            "definition_id": view["skillId"], "kind":"agent.skill", "scope_type":"workspace",
            "scope_id":"workspace-a", "target_revision":view["revision"], "reason":"Expired rollback",
        })
        self.assertEqual(result.status_code, 409, result.text)
        denial = result.json()["detail"]
        self.assertEqual(denial["code"], "procedure_expired")
        draft = self.definitions.get_record(denial["definitionReference"]["record_id"])
        self.assertEqual(draft.lifecycle, DefinitionLifecycle.DRAFT)
        self.assertEqual(draft.rollback_of_record_id, original.record_id)
        self.assertEqual(self.definitions.get_record(original.record_id), original)

    def test_schema_exposure_is_authored_contract_not_execution_or_grants(self):
        result = self.client.get("/api/skills/procedure-schema")
        self.assertEqual(result.status_code, 200, result.text)
        schema = result.json()["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("steps", schema["properties"])
        self.assertEqual(self.actions.list_bindings(self.admin), [])

    def test_unbound_helper_fails_closed_for_procedures_and_preserves_ordinary_skills(self):
        definitions = DefinitionRegistryService(DefinitionRegistryStore(self.state))
        install_skill_definition_schema(definitions)
        app = FastAPI()

        @app.middleware("http")
        async def inject(request: Request, call_next):
            request.state.identity_actor = self.admin
            return await call_next(request)

        app.include_router(build_definitions_router(definitions))
        with TestClient(app) as client:
            denied = client.post("/api/definitions/drafts", json=self.request_payload())
            self.assertEqual(denied.status_code, 409, denied.text)
            self.assertEqual(denied.json()["detail"]["code"], "procedure_gate_unavailable")
            ordinary = self.request_payload()
            ordinary["payload"].pop("operational_procedure")
            ordinary["definition_id"] = "ordinary-http"
            created = client.post("/api/definitions/drafts", json=ordinary)
            self.assertEqual(created.status_code, 200, created.text)
            record = created.json()["record"]
            activated = client.post(f"/api/definitions/{record['record_id']}/publish", json={})
            self.assertEqual(activated.status_code, 200, activated.text)
            self.assertEqual(activated.json()["record"]["checksum"], record["checksum"])
            self.assertNotIn("operational_procedure", activated.json()["record"]["payload"])


if __name__ == "__main__":
    unittest.main()
