import copy
import asyncio
import hashlib
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock

from fastapi import HTTPException
from pydantic import ValidationError

from codex_web.models import HomeOperatorCoordinationDispatch, ProjectCoordinationDispatch, QueuedTurn
from codex_web.action_providers import ActionProviderBinding, ActionRequest
from codex_web.services.home_operator_coordination import (
    HomeOperatorCoordinationService, HomeOperatorCoordinationActionProvider,
)


class HomeCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.actor = NS(identity_id="admin", organization_id="local", workspace_id="default")
        self.home = NS(revision=4)
        self.envelope = NS(host_resource_id="resource-host", project_targets=(
            NS(project_id="data", child_profiles=(NS(profile_id="quinn", revision=6),)),))
        self.origin = NS(id="origin", fence=2, execution_id="origin-execution",
            execution_profile_definition=NS(record_id="home-definition"))
        self.target = NS(id="target", fence=3, created_by="admin", project_id="data",
            organization_id="local", workspace_id="default", execution_id="target-execution",
            execution_workspace_id="target-workspace", subject="thread:quinn", resource_ids=("resource-data",),
            agent_profile=NS(profile_id="quinn", profile_revision=6, profile_record_id="quinn-definition"),
            execution_profile_id="repository-write", status="running", deadline_at=200,
            assigned_worker_id="worker-target", lease=NS(fence=3, worker_id="worker-target", expires_at=150),
            repository_scope=NS(writable_repository_ids=("resource-data",), read_only_repository_ids=("resource-core",)))
        self.profile = NS(profile_id="quinn", revision=6, record_id="quinn-definition", execution_profile_id="repository-write")
        self.worker = NS(lifecycle="active", organization_id="local", workspace_id="default")
        self.binding = NS(assignment_id="target", execution_id="target-execution",
            execution_workspace_id="target-workspace", subject="thread:quinn")
        self.assignments = {"origin": self.origin, "target": self.target}
        self.message = "Finish the existing MR; do not create another one."
        self.proof = HomeOperatorCoordinationDispatch(intent_id="intent", assignment_id="origin", fence=2,
            home_revision=4, target_assignment_id="target", target_fence=3,
            message_sha256=hashlib.sha256(self.message.encode()).hexdigest())
        self.queued = QueuedTurn(id="queued", thread_id="quinn-thread", project_id="data", message=self.message,
            execution_id="home-coordinate-input:intent", created_at=100, source="home-operator-coordination",
            agent_profile_actor_id="admin", agent_profile_id="quinn", agent_profile_revision=6,
            execution_profile_id="repository-write", repository_resource_id="resource-data",
            writable_repository_resource_ids=("resource-data",), read_only_repository_resource_ids=("resource-core",),
            project_coordination_dispatch=self.proof)
        self.intent = NS(id="intent", idempotency_key="home-coordinate:origin:2:one-original", binding_id="binding", action_id="operator.project.thread.message", status="succeeded",
            provider_type="home-operator-coordination", provider_instance="canonical", resource_ids=("resource-host",),
            requested_by="admin", execution_id="origin-execution", organization_id="local", workspace_id="default", project_id="home",
            request=NS(parameters={"admission_key":"home-coordinate:origin:2:one-original", "assignment_id":"origin", "fence":2, "home_revision":4,
                "definition_record_id":"home-definition", "target_assignment_id":"target", "target_fence":3,
                "project_id":"data", "thread_id":"quinn-thread", "message":self.message}))
        self.broker = NS(execution_workers=NS(store=NS(assignment=self.assignments.get, worker=lambda key:self.worker)),
            execution_profiles=NS(agent_profiles=NS(resolve_for_execution=Mock(return_value=(self.profile,None)))),
            authority=NS(evaluate=Mock(return_value=NS(outcome="allow"))),
            action_intents=NS(get=Mock(side_effect=lambda key, actor:self.intent),
                execution=NS(registry=NS(binding=lambda key,actor:NS(enabled=True,credential_ref=None,
                    provider_type="home-operator-coordination",provider_instance="canonical",organization_id="local",
                    workspace_id="default",project_id="home",resource_ids=("resource-host",))))), _clock=lambda:100)
        self.source_guard = Mock(side_effect=lambda assignment, operation:(self.origin,self.actor,self.home,self.envelope))
        self.scope = NS(for_thread=Mock())
        self.service = HomeOperatorCoordinationService(broker=self.broker, origin_guard=self.source_guard,
            thread_scope=self.scope, bootstrap_bindings=NS(get_by_thread=lambda key,actor:self.binding), project_coordination=None)

    def validate(self):
        self.service.validate_queued_delegation(self.queued)

    def test_retained_input_survives_wire_roundtrip_and_legacy_reader_refuses_new_authority(self):
        self.queued = QueuedTurn.model_validate(self.queued.model_dump(mode="json"))
        self.validate()
        self.scope.for_thread.assert_called_with("quinn-thread",self.actor,"data")
        self.assertEqual(self.broker.authority.evaluate.call_args.args[0].resource_ids,("resource-data",))
        with self.assertRaises(ValidationError):
            ProjectCoordinationDispatch.model_validate(self.proof.model_dump())

    def test_changed_home_selection_or_source_custody_holds_original_input(self):
        for change in (lambda:setattr(self.home,"revision",5),lambda:setattr(self.origin,"fence",3),
                       lambda:self.assignments.pop("origin")):
            with self.subTest(change=change), self.assertRaises(HTTPException):
                original=copy.deepcopy((self.home,self.origin,self.assignments))
                change()
                try:self.validate()
                finally:self.home,self.origin,self.assignments=original;self.broker.execution_workers.store.assignment=self.assignments.get

    def test_uncertain_cancelled_foreign_or_modified_action_does_not_unlock_input(self):
        for field,value in (("status","uncertain"),("status","requires_reconciliation"),("status","cancelled"),
                            ("provider_type","reference"),("requested_by","other-admin"),("project_id","other"),
                            ("resource_ids",("resource-other",))):
            with self.subTest(field=field,value=value), self.assertRaises(HTTPException):
                original=getattr(self.intent,field);setattr(self.intent,field,value)
                try:self.validate()
                finally:setattr(self.intent,field,original)
        self.intent.request.parameters["message"]="Different instructions"
        with self.assertRaises(HTTPException):self.validate()

    def test_target_profile_scope_custody_and_lease_are_revalidated(self):
        for obj,field,value in ((self.target,"project_id","other"),(self.target,"created_by","other-admin"),
                (self.target,"fence",4),(self.target,"execution_workspace_id","successor"),
                (self.profile,"revision",7),(self.profile,"record_id","changed-definition"),
                (self.target.lease,"expires_at",99),(self.worker,"lifecycle","quarantined")):
            with self.subTest(field=field,value=value), self.assertRaises(HTTPException):
                original=getattr(obj,field);setattr(obj,field,value)
                try:self.validate()
                finally:setattr(obj,field,original)

    def test_queued_body_source_execution_and_repository_pins_cannot_be_substituted(self):
        for changes in ({"source":"web"},{"message":"Changed instructions"},{"execution_id":"another-execution"},
                {"repository_resource_id":"resource-other"},{"read_only_repository_resource_ids":()},
                {"agent_profile_actor_id":"other-admin"},{"agent_profile_revision":7}):
            with self.subTest(changes=changes), self.assertRaises(HTTPException):
                original=self.queued;self.queued=self.queued.model_copy(update=changes)
                try:self.validate()
                finally:self.queued=original

    def test_unlisted_target_and_denied_current_project_authority_refuse(self):
        self.envelope.project_targets=()
        with self.assertRaises(HTTPException):self.validate()
        self.envelope.project_targets=(NS(project_id="data",child_profiles=(NS(profile_id="quinn",revision=6),)),)
        self.broker.authority.evaluate.return_value=NS(outcome="deny")
        with self.assertRaises(HTTPException):self.validate()


class HomeCoordinationProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        HomeCoordinationTests.setUp(self)
        self.origin.assigned_worker_id = "worker-home"
        self.provider = HomeOperatorCoordinationActionProvider(self.service)
        self.provider_binding = ActionProviderBinding(id="binding", organization_id="local", workspace_id="default",
            project_id="home", resource_ids=("resource-host",), provider_type="home-operator-coordination",
            provider_instance="canonical", enabled=True)
        self.broker.action_intents.execution.registry.binding = lambda key, actor:self.provider_binding
        self.request = ActionRequest(action_id="operator.project.thread.message", organization_id="local", workspace_id="default",
            project_id="home", resource_ids=("resource-host",), requested_by="admin",
            parameters={"admission_key":"home-coordinate:origin:2:one-original", "assignment_id":"origin", "fence":2, "home_revision":4, "definition_record_id":"home-definition",
                "target_assignment_id":"target", "target_fence":3, "project_id":"data", "thread_id":"quinn-thread", "message":self.message})
        self.intent.request = self.request
        self.intent.status = "executing"
        self.intent.attempt = 1
        self.intent.lease = NS(owner="control-plane-broker:worker-home",expires_at=150)
        self.intent.execution_started_at = 95
        self.intent.timeout_seconds = 120
        self.broker.action_intents.store = NS(find_idempotent=lambda *args:self.intent)
        self.effects = []

    async def test_cancelled_original_during_turn_preparation_cannot_admit_input_or_renew_target(self):
        async def start(thread, payload, **kwargs):
            self.assertFalse(kwargs["allow_assignment_renewal"])
            self.intent.status = "cancelled"
            await asyncio.sleep(0)
            kwargs["admission_guard"]()
            self.effects.append(thread)
            return {"queuedId":"queued"}
        self.service.turns = NS(start=start)
        with self.assertRaises(HTTPException):
            await self.provider.execute(self.request,binding=self.provider_binding)
        self.assertEqual(self.effects,[])

    async def test_lost_response_after_one_queue_effect_cannot_replay_uncertain_action(self):
        async def start(thread, payload, **kwargs):
            self.assertFalse(kwargs["allow_assignment_renewal"])
            await asyncio.sleep(0)
            kwargs["admission_guard"]()
            self.effects.append((thread,payload,kwargs))
            raise TimeoutError("Queue response lost after original acceptance")
        self.service.turns = NS(start=start)
        with self.assertRaises(TimeoutError):
            await self.provider.execute(self.request,binding=self.provider_binding)
        self.intent.status = "uncertain"
        with self.assertRaises(HTTPException):
            await self.provider.execute(self.request,binding=self.provider_binding)
        self.assertEqual(len(self.effects),1)
        thread,payload,kwargs=self.effects[0]
        self.assertEqual(thread,"quinn-thread")
        self.assertEqual(kwargs["project_coordination_dispatch"].intent_id,"intent")
        self.assertEqual(kwargs["execution_id"],"home-coordinate-input:intent")
        self.assertEqual(payload.read_only_repository_resource_ids,("resource-core",))
