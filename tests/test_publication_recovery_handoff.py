from __future__ import annotations

import asyncio
import subprocess
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from codex_web.action_intents import (ActionIntent, ActionIntentReceipt, ActionIntentVerificationReceipt,
    ActionIntentRetryPolicy, ActionDecisionSnapshot, ActionIntentState)
from codex_web.action_providers import ActionDefinition, ActionRequest, ActionResult, ActionVerification
from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.api.control_plane_broker import build_control_plane_broker_router
from codex_web.code_hosts import CodeHostCheckFact, CodeHostPullRequestFact, CodeHostValidationRunFact
from codex_web.execution_workspaces import (ExecutionWorkspaceAcquire, PublicationRecoveryHandoffRequest,
    PublicationRecoveryIntentScope)
from codex_web.execution_workers import ExecutionWorker
from codex_web.execution_subjects import ExecutionSubject
from codex_web.models import WorkItemState, TaskSourceIdentity
from codex_web.resources import RepositoryExecutionScope
from codex_web.security import ExecutionSecurityPolicy, SecurityTrustDecision
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.control_plane_broker import ControlPlaneBrokerService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.execution_profile_definitions import install_execution_profile_definitions
from codex_web.services.publication_recovery import (ASSIGNMENTS, WS, LEASES, WORKERS,
    PublicationRecoveryConflict, intent_guard, scope_defaults, scope_rows)
from codex_web.services.publication_recovery_handoff import PublicationRecoveryHandoffService
from codex_web.services.task_sources import TaskSourceSnapshot
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.execution_workers import ExecutionWorkerStore
import tests.test_control_plane_broker as broker_fixtures
import tests.test_execution_workspaces as workspace_fixtures


class PublicationRecoveryHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = workspace_fixtures.ExecutionWorkspaceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store = self.fixture.sqlite
        self.actor = self.fixture.actor
        self.workspaces = self.fixture.service
        registry = DefinitionRegistryService(DefinitionRegistryStore(self.store))
        self.profiles = install_execution_profile_definitions(registry)
        _, self.pin = self.profiles.resolve("repository-write")
        self.workspace = self.workspaces.acquire(ExecutionWorkspaceAcquire(
            subject=ExecutionSubject(kind="thread_bootstrap", ref="handoff"),
            execution_id="execution-broker-test", project_id="home", resource_ids=(self.fixture.repo.id,),
            repository_resource_id=self.fixture.repo.id, ttl_seconds=600), actor=self.actor)
        # A real isolated Git head exercises the mutation-time filesystem precondition.
        path = self.workspace.path
        for args in (("init", "-b", self.workspace.branch_name), ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "--allow-empty", "-m", "fixture")):
            subprocess.run(["git", "-C", path, *args], check=True, capture_output=True)
        self.head = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        self.workers_store = ExecutionWorkerStore(self.store)
        self.workers_store.load()
        self.assignment = broker_fixtures.ControlPlaneBrokerTests._assignment().model_copy(update={
            "project_id": "home", "execution_profile_id": "repository-write", "execution_profile_definition": self.pin,
            "execution_workspace_id": self.workspace.id, "subject": self.workspace.subject,
            "resource_ids": self.workspace.resource_ids,
            "repository_scope": RepositoryExecutionScope(organization_id="local", workspace_id="default", project_id="home",
                writable_repository_ids=self.workspace.writable_repository_ids, source="single_repository", source_ref=self.fixture.repo.id),
            "agent_profile": AgentProfileExecutionBinding(profile_id="james", role_id="james", profile_revision=1, profile_record_id="james-r1"),
        })
        self.store.record_apply(ASSIGNMENTS, upserts={self.assignment.id: self.assignment.model_dump(mode="json")})
        worker = ExecutionWorker(id="worker-1", organization_id="local", workspace_id="default", service_identity_id="service-worker",
            version="fixture", capabilities=("command_execution", "git"), registered_by="fixture",
            supported_execution_contract_versions=(self.assignment.execution_contract_version,))
        self.store.record_apply(WORKERS, upserts={worker.id: worker.model_dump(mode="json")})
        self.workspace = self.workspaces.authorize_publication_recovery(self.workspace.id, work_item_ref="group/app#42",
            repository_resource_id=self.fixture.repo.id, assignment_id=self.assignment.id, assignment_fence=1,
            source_execution_id=self.assignment.execution_id, source_subject=self.assignment.subject,
            assignment_deadline_at=self.assignment.deadline_at, actor=self.actor)
        self.prior = self.workspace.publication_recovery
        self.old = self.fixture.work_item.model_copy(update={"current_stage": "closed", "closed_at": time.time(),
            "terminal_outcome": "completed", "implementation_owner": "james", "artifact_state": "merged_main",
            "source_identity": TaskSourceIdentity(source_type="github", source_instance="https://api.github.com", external_id="group/app#42", revision="old")})
        self.target = self.old.model_copy(update={"ref": "group/app#43", "closed_at": None, "current_stage": "implementation_active",
            "terminal_outcome": None, "source_identity": self.old.source_identity.model_copy(update={"external_id": "group/app#43"})})
        self.store.record_replace("work_item_states", {s.ref: s.model_dump(mode="json") for s in (self.old, self.target)})
        self.action_store = ActionIntentStore(self.store)
        self.action_store.load()
        self.intents = {}
        self.receipts = []
        self.verifications = []
        for action, output, parameters in (
            ("code-host.branch.publish", {"head_revision": self.head, "branch": "codex/owned"}, {"head_revision": self.head, "execution_workspace_id": self.workspace.id}),
            ("code-host.pull-request.upsert", {"change_request_number": 10, "head": "codex/owned"}, {"body": "Refs group/app#42", "head": "codex/owned"}),
            ("code-host.pull-request.merge", {"head_sha": self.head, "merge_commit_sha": "a" * 40, "pull_request_number": 10}, {}),
        ):
            intent = self.make_intent(action)
            intent = intent.model_copy(update={"request": intent.request.model_copy(update={"parameters": parameters})})
            receipt = ActionIntentReceipt(intent_id=intent.id, attempt=1, provider_type="github", provider_instance="github.com",
                action_id=action, idempotency_key=intent.idempotency_key, correlation_id="receipt-correlation",
                result=ActionResult(provider_binding_id="github", action_id=action, status="succeeded", started_at=time.time(), completed_at=time.time(), output=output))
            verification = ActionIntentVerificationReceipt(intent_id=intent.id, verified=True, provider_verification=ActionVerification(verified=True))
            intent = intent.model_copy(update={"last_receipt_id": receipt.id, "last_verification_id": verification.id})
            self.intents[action] = intent
            self.receipts.append(receipt)
            self.verifications.append(verification)
        self.action_store.update(lambda s: ActionIntentState(intents=list(self.intents.values()), receipts=self.receipts, verifications=self.verifications))
        self.source_state = "closed"
        self.check_hook = None
        self.checks_good = True
        async def source_read(identity):
            return TaskSourceSnapshot(identity=identity.model_copy(update={"revision": "terminal-authoritative-r2"}), source_state=self.source_state)
        async def checks(binding, repo, revision, *, actor):
            if self.check_hook:
                hook, self.check_hook = self.check_hook, None
                hook()
            return (CodeHostCheckFact(external_id=revision, name="required-check", state="completed",
                                     conclusion="success" if self.checks_good else "failure", revision=revision),)
        async def pull_request(*args, **kwargs):
            return CodeHostPullRequestFact(external_id="10", number=10, title="Implementation", state="closed", merge_revision="a" * 40)
        async def validation_runs(binding, repo, revision, *, actor):
            await checks(binding, repo, revision, actor=actor)
            from tests.publication_recovery_ci_fixtures import qualified_runs
            runs = list(qualified_runs(revision, recovery=revision == "a" * 40))
            if not self.checks_good:
                runs[0] = runs[0].model_copy(update={"checks": (runs[0].checks[0].model_copy(update={"conclusion": "failure"}),) + runs[0].checks[1:]})
            return tuple(runs)
        self.worker_service = SimpleNamespace(store=self.workers_store)
        # Service tests use explicit worker observations. Real executable-config
        # regressions exercise the production isolation backend separately.
        self.git_observation = {'head': self.head, 'branch': self.workspace.branch_name, 'dirty': ''}
        self.broker = SimpleNamespace(
            local_execution_worker=SimpleNamespace(inspect_git_metadata=lambda *args, **kwargs: dict(self.git_observation)),
            execution_workspaces=self.workspaces,
            execution_profiles=self.profiles,
            action_intents=SimpleNamespace(store=self.action_store, execution_workers=self.worker_service,
                                          _work_item_requirements=lambda *args: ()),
            work_items=SimpleNamespace(task_source_registry=SimpleNamespace(resolve=lambda *args, **kwargs: SimpleNamespace(read=source_read))),
            code_hosts=SimpleNamespace(checks=checks, pull_request=pull_request, validation_runs=validation_runs),
            _authorize=lambda *args, **kwargs: None,
            _state_for_target=lambda assignment, ref: WorkItemState.model_validate(self.store.record_get("work_item_states", ref)),
            _resolve_operation=lambda *args: SimpleNamespace(operation=None),
            _code_host_binding=lambda **kwargs: ("github", self.fixture.repo.id),
        )
        self.service = PublicationRecoveryHandoffService(self.broker)
        self.request = PublicationRecoveryHandoffRequest(work_item_ref=self.target.ref, expected_prior_scope=self.prior,
            expected_assignment_id=self.assignment.id, expected_assignment_fence=1, execution_workspace_id=self.workspace.id,
            repository_resource_id=self.fixture.repo.id, expected_head_revision=self.head,
            completion_intent_ids=tuple(i.id for i in self.intents.values()), idempotency_key="handoff-once")

    def make_intent(self, action, *, status="succeeded"):
        return ActionIntent(organization_id="local", workspace_id="default", project_id="home", work_item_ref=self.old.ref,
            execution_id=self.assignment.execution_id, binding_id="github", provider_type="github", provider_instance="github.com", action_id=action,
            action_definition=ActionDefinition(action_id=action, title=action), request=ActionRequest(action_id=action,
                organization_id="local", workspace_id="default", project_id="home", resource_ids=(self.fixture.repo.id,)),
            authority_decision=ActionDecisionSnapshot(), policy_decision=ActionDecisionSnapshot(), security_policy=ExecutionSecurityPolicy(),
            security_decision=SecurityTrustDecision(outcome="allow", risk_class="medium", sandbox="workspace-write", network_enabled=False,
                authority_source="fixture", policy_source="fixture"), resource_ids=(self.fixture.repo.id,), idempotency_key=action + str(time.time_ns()),
            timeout_seconds=30, retry_policy=ActionIntentRetryPolicy(), status=status, correlation_id="fixture",
            requested_by=self.actor.identity_id, created_at=time.time(), updated_at=time.time())

    def mutate(self, namespace, key, **changes):
        raw = self.store.record_get(namespace, key)
        self.store.record_apply(namespace, upserts={key: {**raw, **changes}})

    async def test_success_retains_proofs_subject_expiry_and_receipts(self):
        before = self.action_store.load().model_dump(mode="json")
        preview = await self.service.handoff(self.request, actor=self.actor, preview=True)
        self.assertTrue(preview["allowed"])
        self.assertEqual(self.workspaces.get(self.workspace.id, self.actor).publication_recovery, self.prior)
        result = await self.service.handoff(self.request, actor=self.actor)
        history = result["handoff"]
        self.assertEqual(history["source_identity"]["revision"], "terminal-authoritative-r2")
        self.assertCountEqual(history["receipt_ids"], [r.id for r in self.receipts])
        self.assertEqual(history["execution_profile_definition"], self.pin.model_dump(mode="json"))
        self.assertEqual(result["item"]["subject"], self.workspace.subject.model_dump(mode="json"))
        self.assertEqual(result["item"]["publication_recovery"]["expires_at"], self.prior.expires_at)
        self.assertEqual(self.action_store.load().model_dump(mode="json"), before)
        repeat = await self.service.handoff(self.request, actor=self.actor)
        self.assertTrue(repeat["repeated"])
        self.assertEqual(len(repeat["item"]["publication_recovery_history"]), 1)

    async def test_closed_label_without_source_publication_or_validation_is_insufficient(self):
        self.source_state = "open"
        with self.assertRaisesRegex(PublicationRecoveryConflict, "terminal proof"):
            await self.service.handoff(self.request, actor=self.actor)
        self.source_state = "closed"
        self.checks_good = False
        with self.assertRaises(PublicationRecoveryConflict):
            await self.service.handoff(self.request, actor=self.actor)
        self.checks_good = True
        self.mutate(self.action_store.records_namespace, "intents:" + self.intents["code-host.branch.publish"].id, last_receipt_id=None)
        with self.assertRaisesRegex(PublicationRecoveryConflict, "terminal disposition|matching verified"):
            await self.service.handoff(self.request, actor=self.actor)

    async def test_pending_uncertain_and_receipt_free_failed_intents_refuse(self):
        for status in ("pending", "claimed", "executing", "uncertain", "requires_reconciliation", "failed", "cancelled"):
            intent = self.make_intent("code-host.issue.comment", status=status)
            self.action_store.insert(intent)
            with self.subTest(status=status), self.assertRaises(PublicationRecoveryConflict):
                await self.service.handoff(self.request, actor=self.actor)
            self.store.record_apply(self.action_store.records_namespace, upserts={}, deletes=("intents:" + intent.id,))
        self.assertEqual(self.workspaces.get(self.workspace.id, self.actor).publication_recovery, self.prior)

    async def test_tagged_nonrepository_pending_intent_also_protects_scope(self):
        scope=PublicationRecoveryIntentScope(execution_workspace_id=self.workspace.id, recovery=self.prior,
            execution_profile_definition=self.pin)
        intent=self.make_intent('artifact.write',status='pending').model_copy(update={
            'resource_ids':(), 'publication_recovery_scope':scope})
        self.action_store.insert(intent)
        with self.assertRaisesRegex(PublicationRecoveryConflict,'pending'):
            await self.service.handoff(self.request,actor=self.actor)

    async def test_refs_publication_and_separate_verified_closure_use_typed_scope(self):
        upsert = self.intents["code-host.pull-request.upsert"]
        self.assertEqual(upsert.request.parameters["body"], "Refs group/app#42")
        closure = self.make_intent("code-host.issue.update")
        closure = closure.model_copy(update={"request": closure.request.model_copy(update={"parameters": {"issue_number": 42, "state": "closed"}})})
        receipt = ActionIntentReceipt(intent_id=closure.id, attempt=1, provider_type="github", provider_instance="github.com",
            action_id=closure.action_id, idempotency_key=closure.idempotency_key, correlation_id="separate-closure",
            result=ActionResult(provider_binding_id="github", action_id=closure.action_id, status="succeeded", started_at=time.time(),
                completed_at=time.time(), output={"issue_number": 42, "state": "closed"}))
        verification = ActionIntentVerificationReceipt(intent_id=closure.id, verified=True, provider_verification=ActionVerification(verified=True))
        closure = closure.model_copy(update={"last_receipt_id": receipt.id, "last_verification_id": verification.id})
        def append(state):
            state.intents.append(closure); state.receipts.append(receipt); state.verifications.append(verification); return state
        self.action_store.update(append)
        result = await self.service.handoff(self.request, actor=self.actor)
        self.assertIn(receipt.id, result["handoff"]["receipt_ids"])
        self.assertTrue(any(c["name"] == "focused-documentation" and c["inapplicable"] for c in result["handoff"]["validation_checks"]))
        self.assertTrue(any(c["name"] == "qualified-release-promotion" and c["inapplicable"] for c in result["handoff"]["validation_checks"]))

    async def test_arbitrary_refs_mention_with_wrong_typed_issue_is_not_proof(self):
        upsert = self.intents["code-host.pull-request.upsert"]
        self.mutate(self.action_store.records_namespace, "intents:" + upsert.id, work_item_ref="group/app#99")
        with self.assertRaisesRegex(PublicationRecoveryConflict, "matching verified"):
            await self.service.handoff(self.request, actor=self.actor)

    async def test_missing_failed_cancelled_selected_jobs_and_recovery_block(self):
        original = self.broker.code_hosts.validation_runs
        async def mutated(binding, repo, revision, *, actor):
            runs = list(await original(binding, repo, revision, actor=actor))
            if self.job_mutation == "missing":
                runs[0] = runs[0].model_copy(update={"checks": tuple(c for c in runs[0].checks if c.name != "unit-static")})
            elif self.job_mutation in {"failure", "cancelled", "skipped"}:
                runs[0] = runs[0].model_copy(update={"checks": tuple(c.model_copy(update={"conclusion": self.job_mutation})
                    if c.name == "validation-selection" else c for c in runs[0].checks)})
            elif self.job_mutation in {"recovery_pending", "recovery_failed", "recovery_missing"} and revision == "a" * 40:
                if self.job_mutation == "recovery_missing":
                    return (runs[0],)
                runs[-1] = runs[-1].model_copy(update={"state": "in_progress" if self.job_mutation == "recovery_pending" else "completed",
                    "conclusion": None if self.job_mutation == "recovery_pending" else "failure"})
            return tuple(runs)
        self.broker.code_hosts.validation_runs = mutated
        for self.job_mutation in ("missing", "failure", "cancelled", "skipped", "recovery_pending", "recovery_failed", "recovery_missing"):
            with self.subTest(case=self.job_mutation), self.assertRaises(PublicationRecoveryConflict):
                await self.service.handoff(self.request, actor=self.actor)
        self.assertEqual(self.workspaces.get(self.workspace.id, self.actor).publication_recovery, self.prior)

    async def test_hot_scope_admission_reads_bounded_rows_preserves_unrelated_records(self):
        from codex_web.services.publication_recovery import capture_scope
        namespaces = (*scope_defaults(), self.action_store.records_namespace)
        unrelated = {f"unrelated-{i}": {"opaque": i} for i in range(1000)}
        for namespace in namespaces:
            self.store.record_apply(namespace, upserts=unrelated)
        workers = SimpleNamespace(store=self.workers_store, assignment_for_execution=lambda *a, **kw: self.assignment)
        intent = self.make_intent("code-host.issue.comment", status="pending")
        with patch.object(self.store, "record_items", side_effect=AssertionError("hot path scanned collection")), \
                patch.object(self.store, "_decode", wraps=self.store._decode) as decode:
            admission = capture_scope(workers, intent.request, intent.execution_id, self.actor, assignment_id=self.assignment.id)
            intent = intent.model_copy(update={"publication_recovery_scope": admission[0]})
            self.action_store.insert(intent, guard_rows=admission[1], guard=admission[2])
            self.assertLessEqual(decode.call_count, 20)
        from codex_web.action_intents import ActionIntentLease
        intent = self.action_store.get(intent.id).model_copy(update={"status": "claimed",
            "lease": ActionIntentLease(owner="worker", acquired_at=time.time(), expires_at=time.time()+60)})
        self.store.record_apply(self.action_store.records_namespace, upserts={"intents:"+intent.id: intent.model_dump(mode="json")})
        executor = ActionIntentService(self.action_store, SimpleNamespace(), execution_workers=workers)
        with patch.object(self.store, "record_items", side_effect=AssertionError("execution scanned collection")), \
                patch.object(self.store, "_decode", wraps=self.store._decode) as decode:
            executed = executor._mark_executing(intent.id, "worker", self.actor)
            self.assertEqual(executed.status, "executing")
            self.assertLessEqual(decode.call_count, 20)
        for namespace in namespaces:
            for key in ("unrelated-0", "unrelated-999"):
                self.assertEqual(self.store.record_get(namespace, key), unrelated[key])

    async def test_provider_verified_failed_receipt_is_retained_without_relabelling(self):
        intent = self.make_intent("code-host.issue.comment", status="failed")
        receipt = ActionIntentReceipt(intent_id=intent.id, attempt=1, provider_type="github", provider_instance="github.com",
            action_id=intent.action_id, idempotency_key=intent.idempotency_key, correlation_id="failed-original", outcome="failed",
            result=ActionResult(provider_binding_id="github", action_id=intent.action_id, status="failed",
                started_at=time.time(), completed_at=time.time()))
        from codex_web.action_providers import ActionEvidence
        proof = ActionIntentVerificationReceipt(intent_id=intent.id, verified=True,
            provider_verification=ActionVerification(verified=True, evidence=(ActionEvidence(
                evidence_type="provider-terminal-disposition", reference="github:verified-disposition"),)))
        intent = intent.model_copy(update={"last_receipt_id": receipt.id, "last_verification_id": proof.id})
        def append(state):
            state.intents.append(intent); state.receipts.append(receipt); state.verifications.append(proof); return state
        self.action_store.update(append)
        result = await self.service.handoff(self.request, actor=self.actor)
        self.assertIn(receipt.id, result["handoff"]["receipt_ids"])
        self.assertEqual(self.action_store.get(intent.id), intent)

    async def test_authoritative_reconciliation_can_settle_unknown_before_receipt_persistence(self):
        from codex_web.action_providers import ActionEvidence
        intent=self.intents['code-host.pull-request.merge']
        original=next(r for r in self.receipts if r.intent_id==intent.id)
        proof=next(v for v in self.verifications if v.intent_id==intent.id)
        unknown=original.model_copy(update={'id':original.id+'-unknown','outcome':'unknown','result':None,
                                           'received_at':original.result.completed_at})
        settled=original.model_copy(update={'id':original.id+'-reconciled','received_at':proof.verified_at+0.000001,
            'details':{'operation':'provider-state-reconciliation','provider_mutation_replayed':False}})
        verified=proof.model_copy(update={'id':proof.id+'-reconciled','provider_verification':ActionVerification(
            verified=True,evidence=(ActionEvidence(evidence_type='provider-state',reference='github:merge-state:10'),))})
        updated=intent.model_copy(update={'last_receipt_id':settled.id,'last_verification_id':verified.id})
        def append(state):
            state.intents=[updated if i.id==intent.id else i for i in state.intents]
            state.receipts.extend((unknown,settled));state.verifications.append(verified);return state
        self.action_store.update(append)
        result=await self.service.handoff(self.request,actor=self.actor)
        self.assertIn(unknown.id,result['handoff']['receipt_ids'])
        self.assertIn(settled.id,result['handoff']['receipt_ids'])

    async def test_generic_failure_receipts_and_stale_or_missing_verification_do_not_prove_disposition(self):
        from codex_web.action_providers import ActionEvidence
        for status, outcome, result_present, proof_kind in (
                ('failed', 'failed', False, 'none'), ('failed', 'failed', True, 'none'),
                ('failed', 'failed', True, 'stale'), ('failed', 'failed', True, 'generic'),
                ('succeeded', 'unknown', False, 'none')):
            intent = self.make_intent('code-host.issue.comment', status=status)
            result = ActionResult(provider_binding_id='github', action_id=intent.action_id, status='failed',
                                  started_at=time.time(), completed_at=time.time()) if result_present else None
            receipt = ActionIntentReceipt(intent_id=intent.id, attempt=1, provider_type='github', provider_instance='github.com',
                action_id=intent.action_id, idempotency_key=intent.idempotency_key, correlation_id='generic',
                outcome=outcome, result=result, details={'reason': 'ActionRequirementError', 'code': 'maintenance'})
            proof = ActionIntentVerificationReceipt(intent_id=intent.id, verified=True,
                verified_at=receipt.received_at - 1 if proof_kind == 'stale' else time.time(),
                provider_verification=ActionVerification(verified=True, evidence=() if proof_kind == 'generic' else (
                    ActionEvidence(evidence_type='provider-terminal-disposition', reference='fixture-proof'),)))
            intent = intent.model_copy(update={'last_receipt_id': receipt.id,
                'last_verification_id': None if proof_kind == 'none' else proof.id})
            def append(state):
                state.intents.append(intent); state.receipts.append(receipt); state.verifications.append(proof); return state
            self.action_store.update(append)
            with self.subTest(status=status, outcome=outcome, proof=proof_kind), self.assertRaisesRegex(PublicationRecoveryConflict, 'terminal disposition'):
                await self.service.handoff(self.request, actor=self.actor)
            self.store.record_apply(self.action_store.records_namespace, upserts={}, deletes=('intents:' + intent.id,))

    async def test_isolated_git_observation_preserves_clean_dirty_branch_and_head_cas(self):
        for changed in ({'dirty': ' M source.py'}, {'head': 'f' * 40}, {'branch': 'wrong'}):
            original = dict(self.git_observation)
            self.git_observation.update(changed)
            with self.subTest(changed=changed), self.assertRaisesRegex(PublicationRecoveryConflict, 'stale or dirty'):
                await self.service.handoff(self.request, actor=self.actor)
            self.git_observation = original
        self.broker.local_execution_worker = None
        with self.assertRaisesRegex(PublicationRecoveryConflict, 'isolated Git inspection is unavailable'):
            await self.service.handoff(self.request, actor=self.actor)

    async def test_exact_profile_pin_cannot_cross_definition_tenant_scope(self):
        record = self.profiles.registry.get_record(self.pin.record_id)
        for scope_type, scope_id in (('organization', 'foreign'), ('workspace', 'foreign'), ('project', 'foreign')):
            foreign = record.model_copy(update={'scope_type': scope_type, 'scope_id': scope_id})
            with self.subTest(scope_type=scope_type), patch.object(self.profiles.registry, 'get_record', return_value=foreign):
                with self.assertRaises(PublicationRecoveryConflict):
                    await self.service.handoff(self.request, actor=self.actor)

    async def test_stale_scope_fence_head_and_expired_lease_fail(self):
        for changes in ({"expected_head_revision": "b" * 40}, {"expected_assignment_fence": 2},
                        {"expected_prior_scope": self.prior.model_copy(update={"authorized_at": self.prior.authorized_at - 1})}):
            with self.subTest(changes=changes), self.assertRaises(PublicationRecoveryConflict):
                await self.service.handoff(self.request.model_copy(update=changes), actor=self.actor)
        self.mutate(LEASES, self.workspace.lease_id, expires_at=0)
        with self.assertRaises(PublicationRecoveryConflict):
            await self.service.handoff(self.request, actor=self.actor)

    async def test_tenant_project_repository_profile_owner_and_worker_denial(self):
        for namespace, key, changes in (
            ("work_item_states", self.target.ref, {"organization_id": "foreign"}),
            ("work_item_states", self.target.ref, {"project_id": "foreign"}),
            ("work_item_states", self.target.ref, {"resource_ids": ["foreign"]}),
            ("work_item_states", self.target.ref, {"current_owner": "foreign"}),
            (ASSIGNMENTS, self.assignment.id, {"execution_profile_id": "orchestration-only"}),
            (WS, self.workspace.id, {"owner_identity_id": "foreign"}),
            (WORKERS, "worker-1", {"lifecycle": "quarantined"}),
        ):
            original = self.store.record_get(namespace, key)
            self.mutate(namespace, key, **changes)
            with self.subTest(changes=changes), self.assertRaises((PublicationRecoveryConflict, ValueError)):
                await self.service.handoff(self.request, actor=self.actor)
            self.store.record_apply(namespace, upserts={key: original})
        with self.assertRaises(PublicationRecoveryConflict):
            await self.service.handoff(self.request, actor=self.actor.model_copy(update={"organization_id": "foreign"}))

    async def test_mutation_rechecks_new_pending_intent_and_lease(self):
        pending = self.make_intent("code-host.issue.comment", status="pending")
        self.check_hook = lambda: self.action_store.insert(pending)
        with self.assertRaisesRegex(PublicationRecoveryConflict, "pending"):
            await self.service.handoff(self.request, actor=self.actor)
        self.store.record_apply(self.action_store.records_namespace, upserts={}, deletes=("intents:" + pending.id,))
        self.check_hook = lambda: self.mutate(ASSIGNMENTS, self.assignment.id, lease=None)
        with self.assertRaisesRegex(PublicationRecoveryConflict, "lease/fence"):
            await self.service.handoff(self.request, actor=self.actor)

    async def test_concurrent_same_request_has_one_durable_transition(self):
        results = await asyncio.gather(*(self.service.handoff(self.request, actor=self.actor) for _ in range(2)))
        self.assertEqual(results[0]["handoff"], results[1]["handoff"])
        self.assertEqual(len(self.workspaces.get(self.workspace.id, self.actor).publication_recovery_history), 1)
        with self.assertRaisesRegex(PublicationRecoveryConflict, "different transition"):
            await self.service.handoff(self.request.model_copy(update={"expected_assignment_fence": 2}), actor=self.actor)

    async def test_concurrent_different_transitions_cannot_overwrite_each_other(self):
        alternative = self.target.model_copy(update={"ref": "group/app#44"})
        self.store.record_apply("work_item_states", upserts={alternative.ref: alternative.model_dump(mode="json")})
        second = self.request.model_copy(update={"work_item_ref": alternative.ref, "idempotency_key": "alternative"})
        results = await asyncio.gather(self.service.handoff(self.request, actor=self.actor),
                                       self.service.handoff(second, actor=self.actor), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(sum(isinstance(r, PublicationRecoveryConflict) for r in results), 1)
        self.assertEqual(len(self.workspaces.get(self.workspace.id, self.actor).publication_recovery_history), 1)

    async def test_recovery_rollback_start_is_atomic_and_retired_scope_cannot_rollback(self):
        from codex_web.services.action_intents import ActionIntentService
        scope=PublicationRecoveryIntentScope(execution_workspace_id=self.workspace.id, recovery=self.prior,
            execution_profile_definition=self.pin)
        original=self.make_intent('artifact.write').model_copy(update={'publication_recovery_scope':scope})
        self.action_store.insert(original)
        service=object.__new__(ActionIntentService)
        service.store=self.action_store;service.execution_workers=self.worker_service
        running=service._begin_recovery_rollback(original)
        self.assertEqual(running.status,'executing')
        with self.assertRaisesRegex(PublicationRecoveryConflict,'executing'):
            await self.service.handoff(self.request,actor=self.actor)
        self.store.record_apply(self.action_store.records_namespace,upserts={},deletes=('intents:'+original.id,))
        await self.service.handoff(self.request,actor=self.actor)
        self.store.record_apply(self.action_store.records_namespace,upserts={'intents:'+original.id:original.model_dump(mode='json')})
        with self.assertRaisesRegex(PublicationRecoveryConflict,'retired or changed'):
            service._begin_recovery_rollback(original)
        self.assertEqual(self.action_store.get(original.id),original)

    async def test_old_scope_creation_and_execution_are_fenced_atomically(self):
        scope = PublicationRecoveryIntentScope(execution_workspace_id=self.workspace.id, recovery=self.prior, execution_profile_definition=self.pin)
        old = self.make_intent("code-host.issue.comment", status="claimed").model_copy(update={"publication_recovery_scope": scope})
        # A delayed creation was prepared before the handoff, but has not entered the outbox.
        await self.service.handoff(self.request, actor=self.actor)
        def guard(documents):
            intent_guard(documents, assignment_id=self.assignment.id, workspace_id=self.workspace.id,
                actor_id=self.actor.identity_id, expected_scope=scope, expected_profile=self.pin)
        with self.assertRaisesRegex(PublicationRecoveryConflict, "retired or changed"):
            self.action_store.insert(old, guard_rows=scope_rows(self.assignment, self.workspace), guard=guard)
        self.assertIsNone(self.action_store.get(old.id))
        # Seed an adversarial retained old intent and exercise the actual execution admission method.
        from codex_web.action_intents import ActionIntentLease
        old = old.model_copy(update={"lease": ActionIntentLease(owner="worker", acquired_at=time.time(), expires_at=time.time()+60)})
        self.action_store.insert(old)
        executor = ActionIntentService(self.action_store, SimpleNamespace(), execution_workers=self.worker_service)
        with self.assertRaisesRegex(PublicationRecoveryConflict, "retired or changed"):
            executor._mark_executing(old.id, "worker", self.actor)
        self.assertEqual(self.action_store.get(old.id).attempt, 0)

    async def test_old_attribution_is_rejected_by_create_after_handoff(self):
        from codex_web.action_intents import ActionIntentCreate
        from codex_web.services.action_intents import ActionIntentConflictError
        await self.service.handoff(self.request, actor=self.actor)
        workers = SimpleNamespace(store=self.workers_store,
            assignment_for_execution=lambda *args, **kwargs: self.workers_store.assignment(self.assignment.id))
        executor = ActionIntentService(self.action_store, SimpleNamespace(), execution_workers=workers)
        old = self.make_intent("code-host.issue.comment", status="pending")
        with self.assertRaisesRegex(ActionIntentConflictError, "differs from current"):
            executor.create(ActionIntentCreate(binding_id="github", request=old.request,
                execution_id=self.assignment.execution_id, work_item_ref=self.old.ref), actor=self.actor)
        self.assertIsNone(self.action_store.get(old.id))

    async def test_exact_profile_checksum_and_new_owner_change_during_validation_deny(self):
        self.check_hook = lambda: self.mutate("work_item_states", self.target.ref, current_owner="different-owner")
        with self.assertRaisesRegex(PublicationRecoveryConflict, "changed during validation"):
            await self.service.handoff(self.request, actor=self.actor)
        self.mutate("work_item_states", self.target.ref, current_owner="james")
        raw = self.store.get("definition_registry")
        raw["records"][0]["checksum"] = "f" * 64
        self.store.put("definition_registry", raw)
        with self.assertRaises(ValueError):
            await self.service.handoff(self.request, actor=self.actor)

    async def test_operator_api_preview_confirmation_and_denial(self):
        app = FastAPI()
        @app.middleware("http")
        async def actor(request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        app.include_router(build_control_plane_broker_router(self.broker))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            preview = await client.post("/api/control-plane-broker/publication-recovery/preview", json=self.request.model_dump(mode="json"))
            self.assertEqual(preview.status_code, 200, preview.text)
            denied = await client.post("/api/control-plane-broker/publication-recovery/handoff", json=self.request.model_copy(update={"expected_head_revision":"b"*40}).model_dump(mode="json"))
            self.assertEqual(denied.status_code, 409, denied.text)
            self.assertIn("next_action", denied.json()["detail"])
            result = await client.post("/api/control-plane-broker/publication-recovery/handoff", json=self.request.model_dump(mode="json"))
            self.assertEqual(result.status_code, 200, result.text)

    def test_broker_catalog_advertises_explicit_handoff_preconditions(self):
        operation = next(i for i in ControlPlaneBrokerService.operations() if i.id == "repository.workspace.publication-recovery")
        schema = ControlPlaneBrokerService.public_operation(operation)["handoff_request_body"]
        self.assertIn("expected_prior_scope", schema["required"])
        self.assertIn("expected_assignment_fence", schema["required"])
        self.assertFalse(schema["additionalProperties"])
