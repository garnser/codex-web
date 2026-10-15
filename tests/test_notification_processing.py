import concurrent.futures
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from codex_web.artifact_evidence import EvidenceCreate, EvidenceResult, VerificationCreate
from codex_web.agent_profiles import AgentProfileUpdate
from codex_web.models import BotReplyTarget, QueuedNativeStartReceipt, WorkItemState, WorkItemHandoff
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.notification_outcomes import OUTCOME_METADATA_KEY, OUTCOME_REVIEW_METHOD, original_snapshot_digest
from codex_web.notification_processing import NotificationProcessingRequest
from codex_web.notification_batches import select_notification_batch
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.notification_outcomes import NotificationOutcomeInspection
from codex_web.services.notification_processing import NotificationProcessingService
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.turn_queue import TurnQueueRepository


class NotificationProcessingTests(unittest.TestCase):
    def setUp(self):
        from tests.test_notification_batch_runtime import NotificationBatchRuntimeTests
        self.f = NotificationBatchRuntimeTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        f = self.f
        f.publish()
        # Retain real reply destinations in the actual admitted originals.
        self.items = [row.model_copy(update={"id":"processing-"+row.id, "reply_target": BotReplyTarget(
            thread_id="thread-a", provider="slack", external_conversation_id="fixture-room",
            external_thread_id="fixture-root", message_id="fixture-message", updated_at=1)}) for row in f.items]
        f.items = self.items
        f.attempts = {row.id: f.preflight.retain_queued(row, organization_id="local", workspace_id="default") for row in self.items}
        self.store = f.preflight.store
        plan = f.plan(f.controls.resolve(self.items[0], f.project))
        self.batch = self.store.claim_notification_batch(plan, claim_id="fixture-processing", now=10)
        self.store.consume_notification_native_start(self.batch.id, claim_id=self.batch.claim_id,
            requested_execution_id=self.batch.id, thread_id="thread-a", message=self.batch.native_input())
        self.store.record_notification_native_start(self.batch.id, QueuedNativeStartReceipt(
            admission_claim_id=self.batch.claim_id, requested_execution_id=self.batch.id,
            execution_id="fixture-execution", thread_id="thread-a", provider_id="fixture-provider",
            runtime_id="fixture-runtime", provider_native_session_id="fixture-session",
            provider_native_turn_id="fixture-turn", observed_at=20))
        self.batch = self.store.get_notification_batch(self.batch.id)
        self.work = ModelMapRepository(f.state, namespace="work_item_states",
            legacy_path=Path(f.temp.name)/"work.json", model=WorkItemState)
        for row in f.work.values():
            self.work.put(row.ref, row.model_copy(update={"current_owner": "fixture-owner", "next_action": "Review exact remaining work"}))
        f.controls.work_item = self.work.get
        self.evidence = ArtifactEvidenceService(ArtifactEvidenceStore(f.state))
        self.outcomes = NotificationOutcomeInspection(self.evidence)
        self.service = NotificationProcessingService(preflight=self.store, outcomes=self.outcomes,
            controls=f.controls, work_items=self.work, clock=lambda: 10**12)
        self.args = dict(thread_id="thread-a", project=f.project, actor=f.actor)
        self.queues = TurnQueueRepository(f.state, Path(f.temp.name)/"queue.json")
        self.queues.put("thread-a", [self.items[0].model_copy(update={"id": "concurrent-append"})])

    def proof(self, index=0, disposition="reconciled", reviewed=True, **changes):
        raw = self.batch.plan.original_snapshots_json[index]
        original = json.loads(raw)
        values = dict(project_id=self.f.project.id, work_item_ref=original["work_item_ref"],
            execution_id=self.batch.native_receipt.execution_id, provider=self.batch.native_receipt.provider_id,
            external_id=self.batch.native_receipt.provider_native_turn_id,
            evidence_type="runtime_result", result="pass" if disposition=="reconciled" else "warn",
            summary="Synthetic processing proof", metadata={OUTCOME_METADATA_KEY:json.dumps({
                "schema_version":"1.0", "admission_id":self.batch.id,"original_id":original["id"],
                "original_snapshot_sha256":original_snapshot_digest(raw),"disposition":disposition})})
        values.update(changes)
        item = self.evidence.create_evidence(EvidenceCreate(**values), actor=self.f.actor)
        if reviewed:
            self.evidence.create_verification(VerificationCreate(work_item_ref=item.work_item_ref,
                execution_id=item.execution_id, evidence_ids=(item.id,), method=OUTCOME_REVIEW_METHOD,
                result="verified"), actor=self.f.actor.model_copy(update={"identity_id":"fixture-independent-reviewer"}))
        return item

    def preview(self, index=0):
        return self.service.preview(self.batch.id, **self.args)["originals"][index]

    def request(self, index=0):
        row = self.preview(index)
        return NotificationProcessingRequest(original_id=row["original_id"],
            expected_processing_sha256=row["expected_processing_sha256"])

    def process(self, request=None):
        return self.service.process(self.batch.id, request or self.request(), **self.args)["original"]

    def test_processing_advances_real_descriptor_links_follow_up_and_preserves_queue_originals(self):
        self.proof()
        self.proof(1, "follow_up_required")
        before = self.queues.get("thread-a")
        request = self.request()
        self.assertEqual(self.process(request)["processing_state"], "processed_current")
        follow_up = self.process(self.request(1))
        self.assertEqual(follow_up["follow_up"]["ref"], self.items[1].work_item_ref)
        self.assertEqual(follow_up["follow_up"]["current_owner"], "fixture-owner")
        self.assertEqual(follow_up["reply"]["delivery_state"], "not_sent")
        self.assertEqual(follow_up["reply"]["destination"], self.items[1].reply_target.model_dump(mode="json"))
        self.assertEqual(self.queues.get("thread-a"), before)
        batch = self.store.get_notification_batch(self.batch.id)
        self.assertEqual(batch.schema_version, "1.2")
        self.assertEqual(len(batch.processing), 2)
        self.assertEqual(batch.plan, self.batch.plan)
        self.assertEqual(batch.native_receipt, self.batch.native_receipt)
        self.assertTrue(all(self.store.get(key).status == "reconciliation_required" for key in batch.attempt_ids))
        self.assertEqual(self.process(request)["historical_receipt"], self.preview()["historical_receipt"])
        self.service.preflight = ExecutionPreflightStore(SQLiteStateStore(self.f.state.path))
        self.assertEqual(self.preview()["processing_state"], "processed_current")

    def test_unreviewed_proposal_unresolved_and_self_review_do_not_process(self):
        item = self.proof(reviewed=False)
        self.assertFalse(self.preview()["can_process"])
        with self.assertRaises(HTTPException): self.process()
        self.evidence.create_verification(VerificationCreate(work_item_ref=item.work_item_ref,
            execution_id=item.execution_id, evidence_ids=(item.id,), method=OUTCOME_REVIEW_METHOD,
            result="verified"), actor=self.f.actor)
        self.assertFalse(self.preview()["can_process"])
        self.assertEqual(self.store.get_notification_batch(self.batch.id).processing, ())

    def test_invalidation_after_processing_preserves_history_and_never_reprocesses(self):
        item = self.proof()
        request = self.request()
        receipt = self.process(request)["historical_receipt"]
        self.evidence.store.update(lambda state: state.model_copy(update={"evidence":[
            row.model_copy(update={"result":EvidenceResult.FAIL}) if row.id==item.id else row for row in state.evidence]}))
        view = self.preview()
        self.assertEqual(view["processing_state"], "historical_requires_reconciliation")
        self.assertEqual(view["historical_receipt"], receipt)
        self.assertFalse(view["can_process"])
        self.assertEqual(self.process(request)["processing_state"], "historical_requires_reconciliation")

    def test_stale_review_conflicting_proof_expiry_and_changed_original_refuse_atomic_commit(self):
        self.proof()
        request = self.request()
        self.proof(disposition="follow_up_required")
        with self.assertRaises(HTTPException): self.process(request)
        self.assertEqual(self.store.get_notification_batch(self.batch.id).processing, ())
        self.proof(1, retention_expires_at=100)
        self.assertFalse(self.preview(1)["can_process"])
        self.proof(2)
        request = self.request(2)
        key = "a/" + self.batch.attempt_ids[2]
        self.f.state.record_update(self.store.NAMESPACE, key,
            lambda raw: {**raw,"queued_admission_claim_id":"competing-claim"}, default=None)
        with self.assertRaises(HTTPException): self.process(request)

    def test_follow_up_requires_current_actionable_owner_and_stale_follow_up_refuses(self):
        self.proof(disposition="follow_up_required")
        request = self.request()
        ref = self.items[0].work_item_ref
        self.work.update(ref, lambda row: row.model_copy(update={"next_action":"Changed exact action"}))
        with self.assertRaises(HTTPException): self.process(request)
        self.work.update(ref, lambda row: row.model_copy(update={"current_owner":None}))
        self.assertIn("actionable_follow_up_owner_and_action_required", self.preview()["blockers"])

    def test_current_controls_revocation_tenant_profile_and_reply_scope_refuse(self):
        self.proof()
        request = self.request()
        with patch.object(self.f.controls, "resolve_processing", return_value=None):
            with self.assertRaises(HTTPException): self.process(request)
        for actor in (self.f.actor.model_copy(update={"workspace_id":"foreign"}),
                      self.f.actor.model_copy(update={"roles":()})):
            with self.assertRaises(HTTPException):
                self.service.process(self.batch.id, request, **{**self.args,"actor":actor})
        self.f.profiles.update(self.f.profile.profile_id,
            AgentProfileUpdate(name="Changed profile", reason="Synthetic new profile revision"), actor=self.f.actor)
        # Exact pinned revision remains authoritative; a new revision does not
        # silently replace the admitted revision or revoke its valid access.
        self.assertEqual(self.preview()["original_id"], self.items[0].id)

    def test_http_processing_uses_canonical_thread_scope_and_rejects_injected_outcomes(self):
        from types import SimpleNamespace
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from codex_web.api.turns import build_turns_router
        from codex_web.api.authorization import EXACT_POLICIES
        from codex_web.models import IndexedThread
        from codex_web.services.projects import ProjectService
        from codex_web.services.thread_scope import ThreadScopeService
        from codex_web.storage.agent_sessions import AgentSessionStore
        from codex_web.storage.thread_index import ThreadIndexRepository
        self.proof()
        index = ThreadIndexRepository(self.f.state, Path(self.f.temp.name)/"threads.json")
        index.upsert(IndexedThread(id="thread-a",name="Fixture",project_id=self.f.project.id))
        scope = ThreadScopeService(ProjectService(SimpleNamespace(load=lambda:[self.f.project])),
            SimpleNamespace(store=AgentSessionStore(index.store)), index)
        app = FastAPI()
        @app.middleware("http")
        async def identity(request, call_next):
            request.state.identity_actor = self.f.actor
            return await call_next(request)
        app.include_router(build_turns_router(SimpleNamespace(),scope,batch_processing=self.service))
        path = f"/api/threads/thread-a/notification-batches/{self.batch.id}/processing"
        revision = self.f.state.namespace_revision(self.store.NAMESPACE)
        with TestClient(app) as client:
            preview = client.get(path)
            self.assertEqual(preview.status_code,200)
            self.assertEqual(self.f.state.namespace_revision(self.store.NAMESPACE),revision)
            body = self.request().model_dump(mode="json")
            self.assertEqual(client.post(path,json={**body,"outcome":"reconciled"}).status_code,422)
            response = client.post(path,json=body)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()["original"]["processing_state"],"processed_current")
            self.assertEqual(client.get(path.replace('thread-a','foreign')).status_code,404)
        policy = EXACT_POLICIES[('POST','/api/threads/{thread_id}/notification-batches/{batch_id}/processing')]
        self.assertEqual(policy.capability,'api.threads.write')
        self.assertEqual(policy.required_assurance,'mfa')

    def test_competing_members_duplicate_calls_and_restart_commit_once_without_lost_updates(self):
        self.proof(); self.proof(1, "follow_up_required")
        first, second = self.request(), self.request(1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(self.process, [first,second,first]))
        self.assertTrue(all(row["processing_state"]=="processed_current" for row in results))
        self.assertEqual(len(self.store.get_notification_batch(self.batch.id).processing), 2)
        original_mutate = self.f.state.record_mutate_many
        def lost_response(*args, **kwargs):
            original_mutate(*args, **kwargs)
            raise OSError("Synthetic response lost after commit")
        self.proof(2); third = self.request(2)
        with patch.object(self.f.state,"record_mutate_many",side_effect=lost_response):
            with self.assertRaises(OSError): self.process(third)
        self.assertEqual(self.process(third)["processing_state"], "processed_current")
        self.assertEqual(len(self.store.get_notification_batch(self.batch.id).processing), 3)

    def test_disabled_admission_does_not_enable_replay_and_processing_remains_explicit(self):
        self.proof()
        self.f.publish(value=False)
        self.assertFalse(self.f.controls.policy_for(self.f.actor,self.f.project)["enabled"])
        self.assertEqual(self.process()["processing_state"], "processed_current")

    def legacy_admission(self):
        # Build the real supported 1.0 planning/admission path without a control
        # snapshot. Current published controls exist but cannot attest its past.
        items = [row.model_copy(update={"id":"legacy-"+row.id}) for row in self.items]
        attempts = {row.id:self.f.preflight.retain_queued(row, organization_id="local",
            workspace_id="default") for row in items}
        plan = select_notification_batch(items, anchor_id=items[0].id,
            attempts_by_original_id=attempts, verified_notification_ids=frozenset(attempts),
            authority_scope_sha256_by_original_id={key:"a"*64 for key in attempts},
            organization_id="local",workspace_id="default",notification_source_origin="gitlab",
            max_members=3,max_context_bytes=65536)
        self.assertEqual(plan.schema_version,"1.0")
        self.assertIsNone(plan.control_snapshot)
        batch = self.store.claim_notification_batch(plan,claim_id="legacy-fixture-claim",now=10)
        self.store.consume_notification_native_start(batch.id,claim_id=batch.claim_id,
            requested_execution_id=batch.id,thread_id="thread-a",message=batch.native_input())
        self.store.record_notification_native_start(batch.id,QueuedNativeStartReceipt(
            admission_claim_id=batch.claim_id,requested_execution_id=batch.id,
            execution_id="legacy-execution",thread_id="thread-a",provider_id="fixture-provider",
            runtime_id="fixture-runtime",provider_native_session_id="legacy-session",
            provider_native_turn_id="legacy-turn",observed_at=20))
        self.batch = self.store.get_notification_batch(batch.id)
        return self.batch

    def test_legacy_missing_original_definition_attestation_refuses_despite_current_controls_and_verified_proof(self):
        legacy = self.legacy_admission()
        self.proof()
        self.assertIsNotNone(self.f.controls.resolve_processing(legacy.plan.members[0],self.f.project))
        before = self.f.state.namespace_revision(self.store.NAMESPACE)
        row = self.preview()
        self.assertEqual(row["outcome"],"reconciled")
        self.assertIn("original_control_definition_evidence_unavailable",row["blockers"])
        self.assertFalse(row["can_process"])
        with self.assertRaises(HTTPException) as error: self.process(self.request())
        self.assertEqual(error.exception.status_code,409)
        self.assertEqual(self.f.state.namespace_revision(self.store.NAMESPACE),before)
        self.assertEqual(self.store.get_notification_batch(legacy.id),legacy)
        for attempt_id,raw in zip(legacy.attempt_ids,legacy.plan.original_snapshots_json,strict=True):
            self.assertEqual(self.store.get(attempt_id).queued_turn_snapshot,json.loads(raw))

    def test_legacy_reads_survive_restart_without_synthesizing_control_pins(self):
        legacy = self.legacy_admission()
        self.proof(disposition="follow_up_required")
        restarted = ExecutionPreflightStore(SQLiteStateStore(self.f.state.path))
        self.service.preflight = restarted
        row = self.preview()
        self.assertEqual(row["processing_state"],"review_required")
        self.assertFalse(row["can_process"])
        from codex_web.services.notification_batch_inspection import NotificationBatchInspection
        detail = NotificationBatchInspection(restarted,outcomes=self.outcomes,clock=lambda:10**12).detail(
            legacy.id,**self.args)
        self.assertEqual(detail["schema_version"],"1.0")
        self.assertIsNone(detail["control_snapshot"])
        self.assertEqual(detail["originals"][0]["outcome"],"follow_up_required")
        self.assertEqual(detail["originals"][0]["original"],json.loads(legacy.plan.original_snapshots_json[0]))

    def test_current_admission_processing_retains_actual_original_and_current_definition_attribution(self):
        self.proof()
        retained = self.batch.plan.control_snapshot
        self.assertIsNotNone(retained)
        receipt = self.process()["historical_receipt"]
        current = json.loads(receipt["control_snapshot_json"])
        self.assertEqual(current["execution_definition_ref"],retained.execution_definition_ref.model_dump(mode="json"))
        self.assertEqual(current["agent_profile_record_id"],retained.agent_profile_record_id)
        self.assertEqual(self.store.get_notification_batch(self.batch.id).plan.control_snapshot,retained)

    def test_changed_published_execution_definition_refuses_reviewed_processing(self):
        self.proof()
        request = self.request()
        active = self.f.execution_profiles.record()
        payload = json.loads(json.dumps(active.payload))
        payload['profiles'][0]['description'] += ' Synthetic version change'
        draft = self.f.definitions.create_draft(DefinitionDraftCreate(
            definition_id=active.definition_id, kind=active.kind,
            definition_schema_version=active.definition_schema_version,
            payload=payload,actor='fixture-admin',reason='Synthetic changed canonical definition'))
        self.f.definitions.publish(draft.record_id,DefinitionPublishRequest(actor='fixture-admin'))
        self.assertIn('admitted_profile_or_execution_definition_changed',self.preview()['blockers'])
        with self.assertRaises(HTTPException): self.process(request)
        self.assertEqual(self.store.get_notification_batch(self.batch.id).processing,())

    def test_pending_handoff_is_linked_without_ack_and_rejected_handoff_requires_reconciliation(self):
        self.proof(disposition='follow_up_required')
        ref = self.items[0].work_item_ref
        self.work.update(ref,lambda row:row.model_copy(update={'handoff':WorkItemHandoff(
            from_agent='fixture-owner',to_agent='fixture-recipient',requested_at=10)}))
        request = self.request()
        receipt = self.process(request)
        self.assertEqual(receipt['follow_up']['handoff']['status'],'pending')
        self.assertIsNone(receipt['follow_up']['handoff']['acknowledged_at'])
        self.assertEqual(self.work.get(ref).handoff.status,'pending')
        self.work.update(ref,lambda row:row.model_copy(update={'handoff':row.handoff.model_copy(update={'status':'rejected'})}))
        row = self.preview()
        self.assertEqual(row['processing_state'],'historical_requires_reconciliation')
        self.assertIn('follow_up_handoff_requires_reconciliation',row['blockers'])

    def test_foreign_proof_repository_and_changed_retained_reply_refuse_processing(self):
        self.proof(resource_ids=('foreign-repository',))
        self.assertIn('proof_repository_or_artifact_scope_mismatch',self.preview()['blockers'])
        with self.assertRaises(HTTPException): self.process()
        self.proof(1)
        request = self.request(1)
        key = 'a/'+self.batch.attempt_ids[1]
        def change(raw):
            raw['queued_turn_snapshot']['reply_target']['external_conversation_id']='other-room'
            return raw
        self.f.state.record_update(self.store.NAMESPACE,key,change,default=None)
        with self.assertRaises(HTTPException): self.process(request)
        self.assertEqual(self.store.get_notification_batch(self.batch.id).processing,())

    def test_partial_transaction_write_rolls_back_and_same_review_can_resume(self):
        from codex_web.storage.state_store import state_record_storage_key
        self.proof()
        request = self.request()
        key = state_record_storage_key(self.store.NAMESPACE,'b/'+self.batch.id)
        original_upsert = self.f.state._upsert
        def failed(connection, namespace, payload):
            original_upsert(connection,namespace,payload)
            if namespace==key: raise OSError('Synthetic failure after SQL row write before commit')
        with patch.object(self.f.state,'_upsert',side_effect=failed):
            with self.assertRaises(OSError): self.process(request)
        self.assertEqual(self.store.get_notification_batch(self.batch.id).processing,())
        self.assertEqual(self.process(request)['processing_state'],'processed_current')
