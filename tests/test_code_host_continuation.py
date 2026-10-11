from copy import deepcopy
from types import SimpleNamespace as NS
import time
import unittest

from codex_web.action_intents import ActionIntentStatus
from codex_web.action_providers import ActionRequest
from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.resources import ResourceType
from codex_web.services.action_intents import ActionIntentService, ActionIntentConflictError
from codex_web.services.code_host_action_contract import CodeHostActionContract, CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID
from codex_web.services.code_host_continuation import ChangeRequestContinuation, continuation_context, continuation_for, request_identity


class ContinuationOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.actor = NS(organization_id="org", workspace_id="workspace")
        self.request = ActionRequest(
            action_id=CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID,
            organization_id="org", workspace_id="workspace", project_id="project",
            resource_ids=("repo",), idempotency_key="new-metadata-update",
            parameters=dict(title="Corrected packet", body="corrected", head="feature/47", base="main", draft=True, origin_intent_id="action-intent-origin"),
        )
        old = self.request.model_copy(update={"idempotency_key": "original-creation", "parameters": {k:v for k,v in self.request.parameters.items() if k != "origin_intent_id"}})
        self.origin = NS(id="action-intent-origin", status=ActionIntentStatus.SUCCEEDED,
                         organization_id="org", workspace_id="workspace", project_id="project",
                         binding_id="binding", action_id=self.request.action_id,
                         resource_ids=("repo",), work_item_ref="issue47", execution_id="old-execution",
                         request=old, idempotency_key=old.idempotency_key, provider_type="gitlab",
                         provider_instance="gitlab.example", last_receipt_id="receipt", last_verification_id="verification")
        self.result = NS(status="succeeded", provider_binding_id="binding", action_id=old.action_id,
                         idempotency_key=old.idempotency_key, external_id="623",
                         output=dict(change_request_number=17, resource_id="repo", head="feature/47", base="main"))
        self.receipt = NS(id="receipt", outcome="completed", intent_id=self.origin.id,
                          provider_type="gitlab", provider_instance="gitlab.example",
                          action_id=old.action_id, idempotency_key=old.idempotency_key,
                          provider_external_id="623", result=self.result)
        self.verification = NS(id="verification", verified=True, intent_id=self.origin.id)
        profile = AgentProfileExecutionBinding(profile_id="james", profile_revision=6, profile_record_id="revision6", role_id="james", execution_profile_id="repository-write")
        self.previous = NS(agent_profile=profile, project_id="project", created_by="shared-admin")
        self.current = NS(agent_profile=profile, project_id="project", created_by="shared-admin", status="running",
                          lease=NS(expires_at=time.time()+60, fence=3, worker_id="worker"), fence=3,
                          assigned_worker_id="worker", deadline_at=None, repository_scope=NS(writable_repository_ids=("repo",)))
        self.worker = NS(lifecycle="active", organization_id="org", workspace_id="workspace")
        self.service = object.__new__(ActionIntentService)
        self.service.store = NS(get=lambda _:self.origin,
                               load=lambda **_:NS(receipts=[self.receipt], verifications=[self.verification]))
        self.service.execution_workers = NS(
            assignment_for_execution=lambda eid, **_:self.previous if eid=="old-execution" else self.current,
            store=NS(worker=lambda _:self.worker))
        self.service.execution = NS(resources=NS(get=lambda *_:NS(resource_type=ResourceType.REPOSITORY)))

    def resolve(self):
        return self.service._change_request_continuation(self.request, binding_id="binding", execution_id="new-execution", work_item_ref="issue47", actor=self.actor)

    def test_original_marker_and_target_survive_new_idempotency_key(self):
        before = deepcopy(self.origin.__dict__)
        proof = self.resolve()
        self.assertEqual(proof.marker, CodeHostActionContract.marker_from_key("original-creation"))
        self.assertEqual((proof.external_id, proof.number), ("623",17))
        self.assertEqual(before, self.origin.__dict__)

    def test_scope_identity_and_receipt_failures_are_denied(self):
        cases = [
            ("origin", "status", "failed"), ("origin", "organization_id", "foreign"),
            ("origin", "binding_id", "foreign"), ("origin", "resource_ids", ("foreign",)),
            ("origin", "work_item_ref", "different-issue"), ("origin", "execution_id", None),
            ("receipt", "outcome", "unknown"), ("receipt", "provider_instance", "other.example"),
            ("receipt", "provider_external_id", "999"), ("verification", "verified", False),
            ("current", "agent_profile", None), ("current", "status", "cancelled"),
            ("current", "fence", 4), ("current", "deadline_at", time.time()-1),
            ("worker", "lifecycle", "disabled"), ("worker", "workspace_id", "foreign"),
            ("current", "repository_scope", NS(writable_repository_ids=("other-repo",))),
        ]
        for obj, key, value in cases:
            with self.subTest(obj=obj, key=key):
                self.setUp();setattr(getattr(self,obj), key, value)
                with self.assertRaises(ActionIntentConflictError):self.resolve()

    def test_shared_requester_cannot_continue_another_agents_mr(self):
        self.current.agent_profile = self.current.agent_profile.model_copy(update={"profile_id":"carl", "role_id":"carl"})
        with self.assertRaises(ActionIntentConflictError):self.resolve()

    def test_expired_current_lease_cannot_borrow_original_receipt(self):
        self.current.lease.expires_at=time.time()-1
        with self.assertRaises(ActionIntentConflictError):self.resolve()

    def test_context_is_exact_request_and_binding_and_is_reset(self):
        proof=self.resolve()
        with self.assertRaises(ValueError):continuation_for(self.request,"binding")
        with continuation_context(proof):
            self.assertIs(continuation_for(self.request,"binding"),proof)
            with self.assertRaises(ValueError):continuation_for(self.request,"other-binding")
            changed=self.request.model_copy(update={"parameters":{**self.request.parameters,"body":"different"}})
            self.assertNotEqual(request_identity(changed),proof.request_identity)
            with self.assertRaises(ValueError):continuation_for(changed,"binding")
        with self.assertRaises(ValueError):continuation_for(self.request,"binding")


class ContinuationProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_execution_rechecks_lease_before_any_provider_invocation(self):
        from unittest.mock import AsyncMock
        from codex_web.services.action_providers import ActionRequirementError
        fixture=ContinuationOwnershipTests();fixture.setUp()
        service=fixture.service
        service._requester_actor=lambda _:fixture.actor
        service.execution.execute=AsyncMock()
        intent=NS(request=fixture.request, binding_id="binding", execution_id="new-execution", work_item_ref="issue47")
        fixture.current.lease.expires_at=time.time()-1
        with self.assertRaises(ActionRequirementError):
            await service._execute_provider(intent,actor=fixture.actor)
        service.execution.execute.assert_not_awaited()
        with self.assertRaises(ValueError):continuation_for(fixture.request,"binding")

    async def test_both_providers_update_exact_original_mr_and_preserve_marker(self):
        from tests import test_gitlab_action_provider, test_github_action_provider
        from codex_web.services.action_providers import ActionRequirementError
        fixtures = [(test_gitlab_action_provider.GitLabActionProviderTests, "merge_request_rows", "description", "opened"),
                    (test_github_action_provider.GitHubActionProviderTests, "pull_requests", "body", "open")]
        for fixture_type, rows_name, body_key, state in fixtures:
            with self.subTest(provider=fixture_type.__name__):
                fixture=fixture_type();await fixture.asyncSetUp()
                try:
                    original=fixture._request(CODE_HOST_CHANGE_REQUEST_UPSERT_ACTION_ID,
                                              dict(title="Original",body="Initial packet",head="feature/47",base="main",draft=True))
                    result=await fixture.execution.execute(fixture.binding.id,original,actor=fixture.actor)
                    row=getattr(fixture.client,rows_name)[0];row["state"]=state
                    # GitLab's real API derives this field from the Draft:
                    # title; the minimal fake client does not synthesize it.
                    row["draft"]=True
                    marker=CodeHostActionContract.marker(original)
                    request=original.model_copy(update={"idempotency_key":"fresh-corrected-packet", "parameters":{**original.parameters,"body":"Corrected packet", "origin_intent_id":"action-intent-origin"}})
                    _,_,_,request=fixture.execution.resolve_contract(fixture.binding.id,request,actor=fixture.actor)
                    proof=ChangeRequestContinuation("action-intent-origin",fixture.binding.id,request_identity(request),marker,result.external_id,result.output["change_request_number"])
                    calls=len(fixture.client.credentials)
                    with self.assertRaises(ActionRequirementError):
                        await fixture.execution.execute(fixture.binding.id,request,actor=fixture.actor)
                    self.assertEqual(len(fixture.client.credentials),calls)
                    with continuation_context(proof):
                        updated=await fixture.execution.execute(fixture.binding.id,request,actor=fixture.actor)
                    self.assertEqual(updated.external_id,result.external_id)
                    self.assertEqual(updated.output["origin_intent_id"],"action-intent-origin")
                    self.assertIn(marker,row[body_key]);self.assertIn("Corrected packet",row[body_key])
                    self.assertTrue((await fixture.execution.verify(fixture.binding.id,updated,actor=fixture.actor)).verified)
                    row["id"]=999
                    with continuation_context(proof),self.assertRaises(ActionRequirementError):
                        await fixture.execution.execute(fixture.binding.id,request,actor=fixture.actor)
                    self.assertEqual(len(getattr(fixture.client,rows_name)),1)
                finally:
                    await fixture.asyncTearDown()
