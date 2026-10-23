"""Private canonical intents/receipts/Evidence; only GitLab HTTP is simulated.

These cases exercise the real interpreter result boundary, not a live procedure
or a new provider read capability. Explicit load_tests excludes inherited tests.
"""
import copy
import asyncio
import threading
import time
import unittest
from types import SimpleNamespace

from codex_web.action_intents import ActionIntentCreate, ActionIntentClaimRequest, ActionDecisionSnapshot
from codex_web.artifact_evidence import ArtifactCreate, sha256_digest
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.artifact_evidence import ArtifactEvidenceService, ArtifactEvidenceConflictError
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.procedure_execution import ProcedureExecutionService, ProcedureExecutionError
from codex_web.services.code_host_action_contract import CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID
from codex_web.services.code_host_action_contract import CODE_HOST_PULL_REQUEST_READY_ACTION_ID, CODE_HOST_PULL_REQUEST_READY_EVIDENCE
from codex_web.action_providers import ActionEvidence
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.definitions import DefinitionReference
from codex_web.execution_workers import ExecutionAssignmentCreate, ExecutionWorkerRegister, AssignmentClaimRequest, AssignmentStartRequest
from codex_web.procedure_execution import ProcedureCheckpoint, ProcedureStepCheckpoint
from codex_web.services.execution_workers import ExecutionWorkerService
from codex_web.storage.execution_workers import ExecutionWorkerStore
from tests.test_gitlab_action_provider import GitLabActionProviderTests


class ProcedureCodeHostResultTests(GitLabActionProviderTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.evidence = ArtifactEvidenceService(ArtifactEvidenceStore(self.sqlite))
        from codex_web.services.artifact_content import ArtifactContentRegistry, ArtifactContentService
        from codex_web.services.local_artifact_content import LocalArtifactContentStore
        from codex_web.services.data_governance import DataGovernanceService
        from codex_web.storage.data_governance import DataGovernanceStore
        from pathlib import Path
        content = ArtifactContentRegistry()
        content.register(LocalArtifactContentStore(Path(self.temp.name)/"private-content"))
        self.evidence.content = ArtifactContentService(content)
        self.evidence.governance = DataGovernanceService(DataGovernanceStore(self.sqlite))
        self.evidence.resources = self.resources
        authority = install_authority_roles(DefinitionRegistryService(DefinitionRegistryStore(self.sqlite)), self.resources)
        self.intents = ActionIntentService(ActionIntentStore(self.sqlite), self.execution,
            authority=authority, identity=self.identity, artifact_evidence=self.evidence)
        self.worker = self.identity.bootstrap_service_actor(identity_id="action-worker", name="Private canonical worker",
            scope=self.actor.tenant, service_scopes=("action-intent:worker", "secret:use", "execution-worker:run"))
        self.client.merge_request_rows.append({"id": 42, "iid": 33, "state": "opened",
            "sha": "a" * 40, "source_branch": "delivery", "target_branch": "main", "draft": False,
            "merge_status": "can_be_merged", "detailed_merge_status": "mergeable",
            "head_pipeline": {"id": 9, "status": "success"}})
        workers = ExecutionWorkerService(ExecutionWorkerStore(self.sqlite), identity=self.identity)
        worker = workers.register(ExecutionWorkerRegister(service_identity_id=self.worker.identity_id,
            pool="private", version="fixture", capabilities=("command_execution",)), actor=self.actor)
        self.assignment = workers.create_assignment(ExecutionAssignmentCreate(subject={"kind":"thread","ref":"private-result-thread"},
            execution_id="procedure-result-execution", project_id="private-project",resource_ids=(self.repository.id,),
            required_capabilities=("command_execution",), execution_contract_version="1.0",
            repository_scope={"organization_id":self.actor.organization_id,"workspace_id":self.actor.workspace_id,
                "project_id":"private-project","writable_repository_ids":[self.repository.id],"source":"explicit"}), actor=self.actor)
        claim = workers.claim(worker.id,AssignmentClaimRequest(lease_seconds=120),actor=self.worker,assignment_id=self.assignment.id)
        self.assignment = workers.start(worker.id,self.assignment.id,
            AssignmentStartRequest(lease_token=claim.lease.lease_token,fence=claim.fence),actor=self.worker)
        self.intents.execution_workers = workers
        self.assignment.procedure_checkpoint = ProcedureCheckpoint(
            definition=DefinitionReference(definition_id="fixture",kind="agent.skill",revision=1,record_id="fixture",
                checksum="c"*64,definition_schema_version="1.0"),actor_identity_id=self.actor.identity_id,
            input_snapshot_id="fixture-input",input_snapshot_checksum="d"*64,resource_context_checksum="fixture",
            started_at=time.time(),deadline_at=time.time()+120,steps={})
        snapshot = self.evidence.create_artifact(ArtifactCreate(project_id=self.assignment.project_id,
            execution_id=self.assignment.execution_id,resource_ids=self.assignment.resource_ids,
            artifact_type="report",name="Private procedure inputs",source="private-procedure-input",
            digest=sha256_digest(b"{}")),actor=self.actor)
        self.evidence.attach_artifact_content(snapshot.id,(b"{}",),actor=self.actor,media_type="application/json")
        self.assignment.procedure_checkpoint.input_snapshot_id = snapshot.id
        self.step = SimpleNamespace(id="merge",worker=None,judgment=None, action=SimpleNamespace(provider_type="gitlab",
            provider_instance="gitlab.example", action_id=CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
            provider_contract_version="1.2"),
            outputs=[SimpleNamespace(name=k, type=t, required=True) for k,t in (
                ("pull_request_number", "integer"), ("head_sha", "string"), ("merge_commit_sha", "string"))],
            expected_evidence=[SimpleNamespace(name="receipt", kind="provider_receipt"),
                               SimpleNamespace(name="verification", kind="verification")])
        self.runner = ProcedureExecutionService(workers=None, skills=None, discovery=None,
            intents=self.intents, evidence=self.evidence, identity=self.identity,
            authority=authority, approvals=None, attention=None, control_actor=self.actor)

    async def original(self):
        request = self._request(CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
            {"pull_request_number": 33, "merge_method": "merge", "expected_head_sha": "a" * 40})
        request.idempotency_key = f"procedure:{self.assignment.id}:{'c' * 64}:merge"
        request.project_id = self.assignment.project_id
        intent = self.intents.create(ActionIntentCreate(binding_id=self.binding.id,
            execution_id=self.assignment.execution_id, request=request,
            policy_decision=ActionDecisionSnapshot(outcome="allow",source="policy:private-procedure-result",
                reason="Controlled canonical fixture permits this exact repository merge")), actor=self.actor)
        self.intents.claim(ActionIntentClaimRequest(worker_id="worker-1", lease_seconds=30),
            intent_id=intent.id, actor=self.worker)
        intent = await self.intents.execute_claimed(intent.id, "worker-1", actor=self.worker)
        self.assertEqual(intent.status, "succeeded", intent.last_error)
        history = self.intents.store.load(intent_id=intent.id)
        receipt = next(r for r in history.receipts if r.id == intent.last_receipt_id)
        return intent, receipt, history

    def project(self, intent, receipt, history):
        return self.runner._provider_result(self.assignment, self.step, intent, receipt, history, self.actor)

    async def test_canonical_original_receipt_and_actual_verification_become_distinct_evidence(self):
        intent, receipt, history = await self.original()
        legacy = copy.deepcopy(receipt.result.output)
        self.assertNotEqual(set(legacy), {"outputs", "evidence"})  # old strict boundary rejects this result
        result = self.project(intent, receipt, history)
        self.assertEqual(result["outputs"], {"pull_request_number":33, "head_sha":"a"*40, "merge_commit_sha":"b"*40})
        proofs = {k:self.evidence.get_evidence(v, actor=self.actor) for k,v in result["evidence"].items()}
        self.assertEqual(proofs["receipt"].evidence_type, "provider_receipt")
        self.assertEqual(proofs["verification"].evidence_type, "artifact_verification")
        for proof in proofs.values():
            self.assertEqual(proof.source, "action-intent:" + intent.id)
            self.assertEqual(proof.execution_id, intent.execution_id)
            self.assertEqual(proof.metadata["receipt_id"], receipt.id)
        self.assertEqual(proofs["verification"].metadata["verification_id"], intent.last_verification_id)
        state = ProcedureStepCheckpoint(attempts=1,action_intent_id=intent.id,started_at=receipt.result.started_at,
            evidence=result["evidence"])
        self.runner._record_outputs(self.assignment,self.step,state,result["outputs"],self.actor)
        self.runner._proof(self.assignment,self.step,state,self.actor)
        artifact = self.evidence.get_artifact(state.output_snapshot_id,actor=self.actor)
        self.assertEqual(artifact.source,f"procedure:{self.assignment.id}:merge")
        self.assertEqual(self.runner._output_values(self.assignment,state,self.actor),result["outputs"])
        self.assertEqual(receipt.result.output, legacy)
        self.assertEqual(self.client.merge_request_merges, 1)

    async def test_checkpoint_retry_reuses_exact_proofs_without_second_provider_effect(self):
        intent, receipt, history = await self.original()
        first = self.project(intent, receipt, history)
        second = self.project(intent, receipt, self.intents.store.load(intent_id=intent.id))
        self.assertEqual(first, second)
        self.assertEqual(self.client.merge_request_merges, 1)
        self.assertEqual(len(self.intents.store.load(intent_id=intent.id).receipts), 1)

    async def test_receipt_cannot_stand_in_for_missing_or_failed_actual_verification(self):
        intent, receipt, history = await self.original()
        for verifications in ([], [v.model_copy(update={"verified":False}) for v in history.verifications]):
            with self.subTest(verifications=bool(verifications)):
                altered = history.model_copy(update={"verifications":verifications})
                with self.assertRaisesRegex(ProcedureExecutionError, "verification_missing"):
                    self.project(intent, receipt, altered)
        self.assertEqual(self.client.merge_request_merges, 1)

    async def test_unknown_wrong_scope_and_wrong_attempt_refuse_without_replay(self):
        intent, receipt, history = await self.original()
        for bad_intent,bad_receipt in (
            (intent.model_copy(update={"status":"uncertain"}),receipt),
            (intent.model_copy(update={"execution_id":"foreign"}),receipt),
            (intent,receipt.model_copy(update={"attempt":receipt.attempt+1})),
            (intent,receipt.model_copy(update={"correlation_id":"foreign"})),
        ):
            with self.assertRaisesRegex(ProcedureExecutionError, "origin_mismatch"):
                self.project(bad_intent,bad_receipt,history)
        self.assertEqual(self.client.merge_request_merges, 1)

    async def test_malformed_projection_and_changed_head_refuse(self):
        intent, receipt, history = await self.original()
        for values in ({"head_sha":"d"*40}, {"pull_request_number":True}, {"merge_commit_sha":""}, {"resource_id":"foreign"}):
            malformed = receipt.model_copy(deep=True)
            malformed.result = malformed.result.model_copy(update={"output":{**malformed.result.output, **values}})
            with self.assertRaisesRegex(ProcedureExecutionError, "projection_invalid"):
                self.project(intent,malformed,history)

    async def test_same_evidence_key_with_changed_original_proof_refuses(self):
        intent, receipt, history = await self.original()
        self.project(intent,receipt,history)
        changed = receipt.model_copy(deep=True)
        changed.result = changed.result.model_copy(update={"execution_id":"changed-provider-result"})
        with self.assertRaises(ArtifactEvidenceConflictError):
            self.project(intent,changed,history)

    async def test_missing_adapter_preserves_legacy_strict_result_shape(self):
        intent, receipt, history = await self.original()
        self.registry._procedure_result_adapters.clear()
        self.assertEqual(self.project(intent,receipt,history), receipt.result.output)

    async def test_contract_drift_and_tenant_registration_do_not_borrow_adapter(self):
        intent, receipt, history = await self.original()
        self.step.action.provider_contract_version = "1.1"
        with self.assertRaisesRegex(ValueError, "contract changed"):
            self.project(intent,receipt,history)
        self.step.action.provider_contract_version = "1.2"
        from codex_web.services.gitlab_action_provider import GitLabActionProvider
        replacement = GitLabActionProvider(self.resources,client=self.client,
            provider_instance="gitlab.example",api_base="https://gitlab.example/api/v4")
        replacement.procedure_result_adapters = ()
        self.registry.register(replacement,tenant_scope=self.actor.tenant)
        self.assertEqual(self.project(intent,receipt,history), receipt.result.output)

    async def test_ready_projection_preserves_exact_target_and_never_implies_review_or_merge(self):
        intent, receipt, history = await self.original()
        request = intent.request.model_copy(update={"action_id":CODE_HOST_PULL_REQUEST_READY_ACTION_ID,
            "parameters":{"pull_request_number":33,"expected_head_sha":"a"*40,
                "expected_base_sha":"b"*40,"expected_base_ref":"main"}})
        result = receipt.result.model_copy(update={"action_id":request.action_id,
            "output":{"resource_id":self.repository.id,**request.parameters,"draft":False,
                "provider_mutation_performed":True,"metadata_sha256":"d"*64},
            "evidence":(ActionEvidence(evidence_type=CODE_HOST_PULL_REQUEST_READY_EVIDENCE,
                metadata={"resource_id":self.repository.id}),)})
        adapter = self.registry.procedure_result_adapter("gitlab","gitlab.example",request.action_id,"1.2",actor=self.actor)
        projected = adapter.project(request,result)
        self.assertEqual(projected,{**request.parameters,"draft":False})
        self.assertNotIn("merge_commit_sha",projected)
        self.assertNotIn("approved",projected)
        for changed in ({"draft":True},{"expected_base_sha":"c"*40},{"provider_mutation_performed":1}):
            with self.assertRaises(ValueError):
                adapter.project(request,result.model_copy(update={"output":{**result.output,**changed}}))

    async def test_blocking_projection_is_off_loop_and_cancellation_joins_actual_owner(self):
        intent, receipt, history = await self.original()
        entered, release, exited = threading.Event(), threading.Event(), threading.Event()
        loop_thread = threading.get_ident()
        def blocked(*args):
            self.assertNotEqual(threading.get_ident(),loop_thread)
            entered.set()
            try:
                if not release.wait(3):
                    raise AssertionError("private fixture release timeout")
            finally:
                exited.set()
        self.runner._provider_result = blocked
        task = asyncio.create_task(self.runner._provider_result_owned(
            self.assignment,self.step,intent,receipt,history,self.actor))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait,2))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task,3)
        self.assertTrue(exited.is_set())


def load_tests(loader, tests, pattern):
    names = [name for name in ProcedureCodeHostResultTests.__dict__ if name.startswith("test_")]
    return unittest.TestSuite(ProcedureCodeHostResultTests(name) for name in names)
