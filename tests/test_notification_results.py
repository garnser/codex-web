import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from codex_web.agent_runtime import AgentRuntimeEvent
from codex_web.artifact_evidence import EvidenceRequirement, EvidenceResult, EvidenceType, VerificationCreate, VerificationResult
from codex_web.identity import AuthenticationAssurance
from codex_web.models import QueuedNativeStartReceipt
from codex_web.notification_batches import NotificationBatchPlan
from codex_web.notification_outcomes import OUTCOME_REVIEW_METHOD, original_snapshot_digest
from codex_web.notification_results import NotificationResultCaptureRequest
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.notification_batch_inspection import NotificationBatchInspection
from codex_web.services.notification_outcomes import NotificationOutcomeInspection
from codex_web.services.notification_results import NotificationResultCapture
from codex_web.services.thread_transcript import ThreadTranscriptService
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class NotificationResultTests(unittest.TestCase):
    def setUp(self):
        from tests.test_notification_batch_runtime import NotificationBatchRuntimeTests
        self.fixture = NotificationBatchRuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.publish()
        control = self.fixture.controls.resolve(self.fixture.items[0], self.fixture.project)
        self.old_plan = self.fixture.plan(control)
        self.plan = self.fixture.plan(control, "1.0")
        self.preflight = self.fixture.preflight.store
        self.batch = self.preflight.claim_notification_batch(self.plan, claim_id="result-fixture", now=100,
            queue_order=tuple(item.id for item in self.plan.members))
        self.preflight.consume_notification_native_start(self.batch.id, claim_id=self.batch.claim_id,
            requested_execution_id=self.batch.id, thread_id="thread-a", message=self.batch.native_input())
        self.receipt = QueuedNativeStartReceipt(admission_claim_id=self.batch.claim_id,
            requested_execution_id=self.batch.id, execution_id="native-execution", thread_id="thread-a",
            provider_native_session_id="thread-a", provider_native_turn_id="native-turn",
            provider_id="openai", runtime_id="codex", observed_at=250)
        self.preflight.record_notification_native_start(self.batch.id, self.receipt)
        self.batch = self.preflight.get_notification_batch(self.batch.id)
        self.transcript = ThreadTranscriptService(self.fixture.state, clock=lambda: 200)
        self.transcript.history.start_turn("thread-a", turn_id="native-execution", message=self.batch.native_input(),
            provider_id="openai", runtime_id="codex", created_at=100)
        self.transcript.record_event("thread-a", AgentRuntimeEvent(event_type="turn/started", provider_native_turn_id="native-turn"))
        self.evidence = ArtifactEvidenceService(ArtifactEvidenceStore(self.fixture.state))
        self.producer = self.fixture.actor.model_copy(update={"identity_id": "service-native-result-capture"})
        self.capture = self.collector(self.preflight, self.transcript, self.evidence)
        self.results = {"schema_version": "1.0", "admission_id": self.batch.id, "results": [
            {"original_id": item.id, "original_snapshot_sha256": original_snapshot_digest(raw),
                "disposition": "reconciled", "reason": "Synthetic reviewed state candidate"}
            for item, raw in zip(self.plan.members, self.plan.original_snapshots_json, strict=True)]}

    def collector(self, preflight, transcript, evidence):
        return NotificationResultCapture(preflight=preflight, transcript=transcript, evidence=evidence,
            project_for=lambda project_id: self.fixture.project, actor_for=lambda batch: self.producer)

    def finish(self, raw=None, phase="final_answer", turn_status="completed"):
        self.transcript.record_event("thread-a", AgentRuntimeEvent(event_type="item/completed", provider_native_turn_id="native-turn",
            payload={"item": {"id": "final-output", "type": "agentMessage", "phase": phase,
                "text": raw if raw is not None else json.dumps(self.results)}}))
        self.transcript.record_event("thread-a", AgentRuntimeEvent(event_type="turn/completed", provider_native_turn_id="native-turn",
            payload={"turn": {"status": turn_status}}))

    def inspector(self):
        return NotificationBatchInspection(self.preflight, outcomes=NotificationOutcomeInspection(self.evidence), clock=lambda: 10**12)

    def detail(self):
        return self.inspector().detail(self.batch.id, thread_id="thread-a", project=self.fixture.project, actor=self.fixture.actor)

    def test_new_input_is_versioned_bounded_and_legacy_shapes_keep_their_input(self):
        self.assertEqual(self.plan.schema_version, "1.2")
        self.assertNotEqual(self.plan.id, self.old_plan.id)
        self.assertIn('"result_contract"', self.plan.native_input())
        self.assertNotIn('"result_contract"', self.old_plan.native_input())
        self.assertNotIn("result_contract_version", self.old_plan.model_dump(mode="json"))
        self.assertEqual(NotificationBatchPlan.model_validate_json(self.plan.model_dump_json()), self.plan)
        with self.assertRaises(ValueError):
            NotificationBatchPlan.model_validate({**self.plan.model_dump(), "schema_version": "1.1"})
        for raw in self.plan.original_snapshots_json:
            self.assertIn(original_snapshot_digest(raw), self.plan.native_input())
        with self.assertRaises(ValueError):
            NotificationBatchPlan.model_validate({**self.plan.model_dump(), "max_context_bytes": 256})

    def test_capture_is_idempotent_across_terminal_replay_and_restart_without_completion(self):
        self.finish()
        before = self.fixture.state.namespace_revision(self.preflight.NAMESPACE)
        result = self.capture.collect(self.batch.id)
        self.assertFalse(result["independently_verified"])
        self.assertEqual(len(result["evidence_ids"]), 3)
        self.assertEqual(self.capture.on_terminal({"method": "turn/completed", "params": {
            "threadId": "thread-a", "turnId": "native-turn"}}), result)
        state = SQLiteStateStore(self.fixture.state.path)
        restored = self.collector(ExecutionPreflightStore(state), ThreadTranscriptService(state),
            ArtifactEvidenceService(ArtifactEvidenceStore(state)))
        self.assertEqual(restored.collect(self.batch.id), result)
        self.assertEqual(len(self.evidence.store.load().evidence), 3)
        self.assertEqual(self.fixture.state.namespace_revision(self.preflight.NAMESPACE), before)
        detail = self.detail()
        self.assertEqual(detail["outcome_counts"]["unresolved"], 3)
        self.assertTrue(all(row["candidate_evidence_ids"] and row["blockers"] == ["independent_review_required"] for row in detail["originals"]))
        self.assertTrue(all(self.preflight.get(key).status == "reconciliation_required" for key in self.batch.attempt_ids))

    def test_original_execution_actor_cannot_independently_review_the_captured_model(self):
        self.finish()
        evidence_id = self.capture.collect(self.batch.id)["evidence_ids"][0]
        payload = VerificationCreate(work_item_ref=self.plan.members[0].work_item_ref, execution_id="native-execution",
            evidence_ids=(evidence_id,), method=OUTCOME_REVIEW_METHOD, result=VerificationResult.VERIFIED)
        self.evidence.create_verification(payload, actor=self.fixture.actor)
        self.assertEqual(self.detail()["originals"][0]["outcome"], "unresolved")
        self.evidence.create_verification(payload, actor=self.fixture.actor.model_copy(update={"identity_id": "independent-verifier"}))
        self.assertEqual(self.detail()["originals"][0]["outcome"], "reconciled")

    def test_captured_proposals_never_satisfy_generic_completion_even_after_review(self):
        self.finish()
        evidence_id = self.capture.collect(self.batch.id)["evidence_ids"][0]
        self.assertTrue(all(item.result == EvidenceResult.INFO for item in self.evidence.store.load().evidence))
        ref = self.plan.members[0].work_item_ref
        self.evidence.create_verification(VerificationCreate(work_item_ref=ref, execution_id="native-execution",
            evidence_ids=(evidence_id,), method=OUTCOME_REVIEW_METHOD, result=VerificationResult.VERIFIED),
            actor=self.fixture.actor.model_copy(update={"identity_id": "independent-verifier"}))
        self.assertEqual(self.detail()["originals"][0]["outcome"], "reconciled")
        for independent in (False, True):
            gate = self.evidence.evaluate(ref, [EvidenceRequirement(id="permissive-completion",
                evidence_type=EvidenceType.RUNTIME_RESULT, accepted_results=(EvidenceResult.INFO, EvidenceResult.PASS),
                independent_verification=independent)], actor=self.fixture.actor)
            self.assertFalse(gate.satisfied)

    def test_unresolved_result_stays_unresolved_and_reason_is_not_copied_into_evidence(self):
        self.results["results"][0].update(disposition="unresolved", reason="sensitive fixture output")
        self.finish()
        self.capture.collect(self.batch.id)
        original = self.detail()["originals"][0]
        self.assertEqual(original["blockers"], ["native_proposal_unresolved"])
        self.assertTrue(original["candidate_evidence_ids"])
        self.assertNotIn("sensitive fixture output", self.evidence.store.load().model_dump_json())

    def test_invalid_foreign_missing_duplicate_and_stale_result_writes_nothing(self):
        valid = json.dumps(self.results)
        cases = ["not JSON", "x" * 32769]
        for field, value in (("admission_id", "notification-admission-" + "f" * 64), ("schema_version", "2.0")):
            changed = json.loads(valid)
            changed[field] = value
            cases.append(json.dumps(changed))
        for changes in ({"original_id": "foreign"}, {"original_snapshot_sha256": "f" * 64}, {"disposition": "success"}):
            changed = json.loads(valid)
            changed["results"][0].update(changes)
            cases.append(json.dumps(changed))
        changed = json.loads(valid)
        changed["results"][1] = changed["results"][0]
        cases.append(json.dumps(changed))
        changed = json.loads(valid)
        changed["results"].pop()
        cases.append(json.dumps(changed))
        cases.append(valid.replace('"schema_version": "1.0"', '"schema_version":"1.0","schema_version":"1.0"'))
        for raw in cases:
            with self.subTest(raw=raw[:80]):
                self.finish(raw)
                with self.assertRaises(ValueError):
                    self.capture.collect(self.batch.id)
                self.assertEqual(self.evidence.store.load().evidence, [])

    def test_failed_nonterminal_unknown_and_other_native_turn_cannot_capture(self):
        self.assertIsNone(self.capture.collect(self.batch.id))
        self.finish(turn_status="failed")
        self.assertIsNone(self.capture.collect(self.batch.id))
        self.assertIsNone(self.capture.on_terminal({"method": "turn/completed", "params": {
            "threadId": "thread-a", "turnId": "foreign-turn"}}))
        self.assertIsNone(self.capture.on_terminal({"method": "turn/failed", "params": {
            "threadId": "thread-a", "turnId": "native-turn"}}))
        self.assertEqual(self.evidence.store.load().evidence, [])

    def test_crash_after_partial_capture_resumes_without_duplicate_evidence(self):
        self.finish()
        create = self.evidence.create_evidence
        calls = 0
        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("fixture crash")
            return create(*args, **kwargs)
        with patch.object(self.evidence, "create_evidence", side_effect=fail_second), self.assertRaises(RuntimeError):
            self.capture.collect(self.batch.id)
        self.assertEqual(len(self.evidence.store.load().evidence), 1)
        self.capture.collect(self.batch.id)
        self.assertEqual(len(self.evidence.store.load().evidence), 3)

    def test_changed_native_output_cannot_overwrite_existing_captured_evidence(self):
        self.finish()
        self.capture.collect(self.batch.id)
        previous = self.evidence.store.load()
        self.results["results"][0]["disposition"] = "follow_up_required"
        self.finish()
        with self.assertRaises(RuntimeError):
            self.capture.collect(self.batch.id)
        self.assertEqual(self.evidence.store.load(), previous)

    def test_operator_scope_assurance_and_expected_native_turn_are_checked_before_capture(self):
        self.finish()
        payload = NotificationResultCaptureRequest(expected_native_turn_id="native-turn")
        arguments = dict(thread_id="thread-a", project=self.fixture.project, actor=self.fixture.actor)
        for change in ({"thread_id": "foreign"}, {"actor": self.fixture.actor.model_copy(update={"workspace_id": "foreign"})}):
            with self.assertRaises(HTTPException):
                self.capture.collect_for_operator(self.batch.id, payload, **{**arguments, **change})
        with self.assertRaises(HTTPException) as denied:
            self.capture.collect_for_operator(self.batch.id, payload, **{**arguments,
                "actor": self.fixture.actor.model_copy(update={"assurance": AuthenticationAssurance.PRIMARY})})
        self.assertEqual(denied.exception.status_code, 403)
        with self.assertRaises(HTTPException):
            self.capture.collect_for_operator(self.batch.id,
                NotificationResultCaptureRequest(expected_native_turn_id="stale-turn"), **arguments)
        self.assertEqual(self.evidence.store.load().evidence, [])
        self.assertEqual(len(self.capture.collect_for_operator(self.batch.id, payload, **arguments)["evidence_ids"]), 3)

    def test_ambiguous_final_messages_and_changed_retained_members_refuse_capture(self):
        self.finish()
        self.transcript.record_event("thread-a", AgentRuntimeEvent(event_type="item/completed", provider_native_turn_id="native-turn",
            payload={"item": {"id": "second-final", "type": "agentMessage", "phase": "final_answer", "text": json.dumps(self.results, indent=2)}}))
        with self.assertRaises(ValueError):
            self.capture.collect(self.batch.id)
        self.assertEqual(self.evidence.store.load().evidence, [])
        self.transcript.record_event("thread-a", AgentRuntimeEvent(event_type="item/completed", provider_native_turn_id="native-turn",
            payload={"item": {"id": "second-final", "type": "agentMessage", "phase": "commentary", "text": "commentary"}}))
        self.fixture.state.record_apply(self.preflight.NAMESPACE, upserts={}, deletes=("a/" + self.batch.attempt_ids[-1],))
        with self.assertRaises(ValueError):
            self.capture.collect(self.batch.id)
        self.assertEqual(self.evidence.store.load().evidence, [])

    def test_http_capture_uses_canonical_scope_and_accepts_no_result_assertion(self):
        from types import SimpleNamespace
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from codex_web.api.turns import build_turns_router
        from codex_web.models import IndexedThread
        from codex_web.services.projects import ProjectService
        from codex_web.services.thread_scope import ThreadScopeService
        from codex_web.storage.agent_sessions import AgentSessionStore
        from codex_web.storage.thread_index import ThreadIndexRepository
        from pathlib import Path

        self.finish()
        index = ThreadIndexRepository(self.fixture.state, Path(self.fixture.temp.name) / "result-index.json")
        index.upsert(IndexedThread(id="thread-a", name="Fixture", project_id=self.fixture.project.id))
        scope = ThreadScopeService(ProjectService(SimpleNamespace(load=lambda: [self.fixture.project])),
            SimpleNamespace(store=AgentSessionStore(index.store)), index)
        app = FastAPI()
        @app.middleware("http")
        async def identity(request, call_next):
            request.state.identity_actor = self.fixture.actor
            return await call_next(request)
        app.include_router(build_turns_router(SimpleNamespace(), scope, batch_results=self.capture))
        path = "/api/threads/thread-a/notification-batches/" + self.batch.id + "/results"
        with TestClient(app) as client:
            self.assertEqual(client.post(path, json={"expected_native_turn_id": "native-turn", "outcome": "success"}).status_code, 422)
            self.assertEqual(client.post(path, json={"expected_native_turn_id": "stale"}).status_code, 409)
            self.assertEqual(client.post(path + "?project_id=foreign", json={"expected_native_turn_id": "native-turn"}).status_code, 404)
            self.fixture.actor = self.fixture.actor.model_copy(update={"assurance": AuthenticationAssurance.PRIMARY})
            self.assertEqual(client.post(path, json={"expected_native_turn_id": "native-turn"}).status_code, 403)
            self.assertEqual(self.evidence.store.load().evidence, [])
            self.fixture.actor = self.fixture.actor.model_copy(update={"assurance": AuthenticationAssurance.MFA})
            first = client.post(path, json={"expected_native_turn_id": "native-turn"})
            self.assertEqual(first.status_code, 200)
            self.assertEqual(client.post(path, json={"expected_native_turn_id": "native-turn"}).json(), first.json())
