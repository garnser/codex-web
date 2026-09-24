from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.action_intents import ActionIntentStatus
from codex_web.approval_requests import ApprovalRequestStatus
from codex_web.automation_definitions import (
    AUTOMATION_KIND,
    AUTOMATION_SCHEMA_VERSION,
)
from codex_web.automation_runs import (
    AutomationRunStatus,
    AutomationRunTrigger,
    AutomationRunTriggerKind,
)
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.automation_definitions import install_automation_definitions
from codex_web.services.automation_execution import AutomationExecutionService
from codex_web.services.automation_runs import AutomationRunService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.storage.automation_runs import AutomationRunStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Identity:
    def __init__(self, actor):
        self.actor = actor
        self.calls = []

    def actor_for_identity(self, identity_id, *, scope):
        self.calls.append((identity_id, scope))
        return self.actor


class _Profiles:
    def __init__(self):
        self.calls = []

    def resolve_for_execution(self, profile_id, *, actor, project_id, revision=None):
        self.calls.append((profile_id, actor.identity_id, project_id, revision))
        return SimpleNamespace(profile_id=profile_id, revision=3), SimpleNamespace(allowed=True)


class _Threads:
    def __init__(self, profiles):
        self.agent_profiles = profiles
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": "thread-automation-1"}


class _Turns:
    def __init__(self):
        self.calls = []

    async def start(self, thread_id, payload, **kwargs):
        self.calls.append((thread_id, payload, kwargs))
        return {"queued": False}


class _Teams:
    def __init__(self):
        self.calls = []

    async def execute(self, team_id, payload, *, actor):
        self.calls.append((team_id, payload, actor))
        return SimpleNamespace(
            plan=SimpleNamespace(blocked=False, reason="ok"),
            coordinator=SimpleNamespace(execution_id="team-exec-1"),
            members=(),
        )


class _Events:
    def event(self, event_id):
        return None


class _ActionProviders:
    def __init__(self):
        self.bindings = [
            SimpleNamespace(
                id="task-source-binding",
                enabled=True,
                provider_type="task-source",
                provider_instance="authoritative",
                project_id="home",
            )
        ]

    def list_bindings(self, actor):
        return list(self.bindings)


class _ActionIntents:
    def __init__(self):
        self.calls = []
        self.histories = {}

    def create(self, payload, *, actor):
        self.calls.append((payload, actor))
        return SimpleNamespace(
            id="action-intent-work-item-1",
            status=ActionIntentStatus.PENDING,
        )

    def history(self, intent_id, actor):
        return self.histories.get(intent_id, {"receipts": []})


class _Approvals:
    def __init__(self):
        self.requests = {}
        self.create_calls = []
        self.consume_calls = []

    async def create_for_identity(
        self,
        payload,
        *,
        requester_identity_id,
        scope,
        request_id=None,
    ):
        self.create_calls.append(
            (payload, requester_identity_id, scope, request_id)
        )
        request = SimpleNamespace(
            id=request_id,
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            project_id=payload.project_id,
            target=payload.target,
            status=ApprovalRequestStatus.PENDING,
        )
        self.requests[request.id] = request
        return request

    def get(self, request_id, *, actor):
        return self.requests[request_id]

    async def consume(self, request_id, payload, *, actor):
        request = self.requests[request_id]
        if request.status != ApprovalRequestStatus.APPROVED:
            raise RuntimeError("approval is not approved")
        if payload.target != request.target:
            raise RuntimeError("approval target mismatch")
        self.consume_calls.append((request_id, payload, actor))
        request.status = ApprovalRequestStatus.CONSUMED
        return request


class _WorkItems:
    def __init__(self):
        self.state_machine = self
        self.states = {
            "group/app#42": SimpleNamespace(
                organization_id="local",
                workspace_id="default",
                project_id="home",
            ),
            "group/other#7": SimpleNamespace(
                organization_id="local",
                workspace_id="default",
                project_id="other",
            ),
        }

    def _work_item_state(self, ref):
        if ref not in self.states:
            raise RuntimeError("Work Item not found")
        return self.states[ref]


class AutomationExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.registry = DefinitionRegistryService(DefinitionRegistryStore(store))
        self.definitions = install_automation_definitions(self.registry)
        self.runs = AutomationRunService(
            AutomationRunStore(store),
            self.definitions,
            clock=lambda: 100.0,
        )
        self.owner = AuthenticationActor(
            identity_id="automation-owner",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
            roles=(MembershipRole.OWNER,),
        )
        self.identity = _Identity(self.owner)
        self.profiles = _Profiles()
        self.threads = _Threads(self.profiles)
        self.turns = _Turns()
        self.teams = _Teams()
        self.work_items = _WorkItems()
        self.action_providers = _ActionProviders()
        self.action_intents = _ActionIntents()
        self.approvals = _Approvals()
        self.service = AutomationExecutionService(
            self.runs,
            identity=self.identity,
            threads=self.threads,
            turns=self.turns,
            teams=self.teams,
            events=_Events(),
            work_items=self.work_items,
            action_intents=self.action_intents,
            action_providers=self.action_providers,
            approvals=self.approvals,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _payload(
        self,
        *,
        target_kind="agent_profile",
        owner=True,
        work_item_policy="reuse_or_create",
        approval_required=False,
    ):
        return {
            "name": "Execute canonical work",
            "lifecycle": "enabled",
            "trigger": {"type": "manual"},
            "target": {"kind": target_kind, "id": "agent-james" if target_kind == "agent_profile" else "team-platform"},
            "instructions": "Inspect the assigned work and execute only authorized changes.",
            "owner_identity_id": "automation-owner" if owner else None,
            "budget": {
                "max_input_tokens": 20000,
                "max_output_tokens": 4000,
                "max_cost_usd": 2.5,
                "max_duration_seconds": 900,
                "max_concurrency": 1,
            },
            "retry": {"max_attempts": 3, "backoff_seconds": 60},
            "work_item_policy": work_item_policy,
            "approval_required": approval_required,
            "failure_attention": True,
        }

    def _publish_and_admit(
        self,
        *,
        target_kind="agent_profile",
        owner=True,
        work_item_policy="reuse_or_create",
        approval_required=False,
    ):
        draft = self.registry.create_draft(
            DefinitionDraftCreate(
                definition_id=f"automation-{target_kind}-{'owner' if owner else 'no-owner'}",
                kind=AUTOMATION_KIND,
                definition_schema_version=AUTOMATION_SCHEMA_VERSION,
                payload=self._payload(
                    target_kind=target_kind,
                    owner=owner,
                    work_item_policy=work_item_policy,
                    approval_required=approval_required,
                ),
                actor="operator",
            )
        )
        self.registry.publish(
            draft.record_id,
            DefinitionPublishRequest(actor="operator"),
        )
        result = self.runs.admit(
            draft.definition_id,
            AutomationRunTrigger(
                kind=AutomationRunTriggerKind.MANUAL,
                source_id="manual:operator:test",
            ),
            organization_id="local",
            workspace_id="default",
            project_id="home",
            idempotency_key=f"{target_kind}-{owner}",
        )
        self.assertTrue(result.launch_allowed)
        return result.run

    async def test_agent_profile_launch_rechecks_owner_and_records_execution(self) -> None:
        run = self._publish_and_admit()

        launched = await self.service.launch(
            run.id,
            organization_id="local",
            workspace_id="default",
            work_item_ref="group/app#42",
            repository_resource_id="repo-app",
        )

        self.assertEqual(launched.status, AutomationRunStatus.RUNNING)
        self.assertEqual(launched.work_item_ref, "group/app#42")
        self.assertEqual(len(launched.execution_ids), 1)
        self.assertTrue(launched.execution_ids[0].startswith("automation-turn-"))
        self.assertEqual(self.identity.calls[0][0], "automation-owner")
        self.assertEqual(
            self.profiles.calls[0][:3],
            ("agent-james", "automation-owner", "home"),
        )
        self.assertEqual(self.threads.calls[0]["repository_resource_id"], "repo-app")
        self.assertEqual(self.turns.calls[0][2]["work_item_ref"], "group/app#42")
        self.assertEqual(
            self.turns.calls[0][2]["actor"].identity_id,
            "automation-owner",
        )

    async def test_approval_required_waits_before_any_side_effect(self) -> None:
        run = self._publish_and_admit(
            work_item_policy="reuse_only",
            approval_required=True,
        )

        waiting = await self.service.launch(
            run.id,
            organization_id="local",
            workspace_id="default",
            work_item_ref="group/app#42",
        )

        self.assertEqual(
            waiting.status,
            AutomationRunStatus.WAITING_FOR_APPROVAL,
        )
        self.assertEqual(waiting.work_item_ref, "group/app#42")
        self.assertIsNotNone(waiting.approval_request_id)
        self.assertEqual(len(self.approvals.create_calls), 1)
        payload, requester, _scope, request_id = self.approvals.create_calls[0]
        self.assertEqual(requester, "automation-owner")
        self.assertEqual(request_id, waiting.approval_request_id)
        self.assertEqual(payload.target.operation, "automation.execute")
        self.assertEqual(payload.target.object_type, "automation_run")
        self.assertEqual(payload.target.object_id, run.id)
        self.assertEqual(
            payload.target.target_digest,
            run.definition_ref.checksum,
        )
        self.assertEqual(self.threads.calls, [])
        self.assertEqual(self.turns.calls, [])
        self.assertEqual(self.action_intents.calls, [])

    async def test_approved_request_is_consumed_before_execution_resumes(self) -> None:
        run = self._publish_and_admit(
            work_item_policy="reuse_only",
            approval_required=True,
        )
        waiting = await self.service.launch(
            run.id,
            organization_id="local",
            workspace_id="default",
            work_item_ref="group/app#42",
        )
        request = self.approvals.requests[waiting.approval_request_id]
        request.status = ApprovalRequestStatus.APPROVED

        results = await self.service.resume_for_approval_request(request)

        self.assertEqual(len(results), 1)
        resumed = results[0]
        self.assertEqual(resumed.status, AutomationRunStatus.RUNNING)
        self.assertEqual(resumed.work_item_ref, "group/app#42")
        self.assertEqual(len(self.approvals.consume_calls), 1)
        consume_id, consume_payload, consumer = self.approvals.consume_calls[0]
        self.assertEqual(consume_id, waiting.approval_request_id)
        self.assertEqual(consume_payload.target, request.target)
        self.assertEqual(consumer.identity_id, "automation-owner")
        self.assertEqual(request.status, ApprovalRequestStatus.CONSUMED)
        self.assertEqual(len(self.turns.calls), 1)

    async def test_rejected_approval_blocks_without_execution(self) -> None:
        run = self._publish_and_admit(
            work_item_policy="reuse_only",
            approval_required=True,
        )
        waiting = await self.service.launch(
            run.id,
            organization_id="local",
            workspace_id="default",
            work_item_ref="group/app#42",
        )
        request = self.approvals.requests[waiting.approval_request_id]
        request.status = ApprovalRequestStatus.REJECTED

        results = await self.service.resume_for_approval_request(request)

        self.assertEqual(len(results), 1)
        blocked = results[0]
        self.assertEqual(blocked.status, AutomationRunStatus.BLOCKED)
        self.assertEqual(
            blocked.block_code,
            "automation_approval_rejected",
        )
        self.assertEqual(self.turns.calls, [])
        self.assertEqual(self.action_intents.calls, [])

    async def test_missing_owner_blocks_before_target_execution(self) -> None:
        run = self._publish_and_admit(owner=False)

        blocked = await self.service.launch(
            run.id,
            organization_id="local",
            workspace_id="default",
        )

        self.assertEqual(blocked.status, AutomationRunStatus.BLOCKED)
        self.assertEqual(blocked.block_code, "automation_launch_blocked")
        self.assertIn("owner_identity_id", blocked.block_reason)
        self.assertEqual(self.threads.calls, [])
        self.assertEqual(self.turns.calls, [])

    async def test_reuse_only_requires_existing_canonical_work_item(self) -> None:
        run = self._publish_and_admit(work_item_policy="reuse_only")

        blocked = await self.service.launch(
            run.id,
            organization_id="local",
            workspace_id="default",
        )

        self.assertEqual(blocked.status, AutomationRunStatus.BLOCKED)
        self.assertIn("reuse_only", blocked.block_reason)
        self.assertEqual(self.threads.calls, [])
        self.assertEqual(self.turns.calls, [])

    async def test_reused_work_item_must_match_run_project(self) -> None:
        run = self._publish_and_admit(work_item_policy="reuse_only")

        blocked = await self.service.launch(
            run.id,
            organization_id="local",
            workspace_id="default",
            work_item_ref="group/other#7",
        )

        self.assertEqual(blocked.status, AutomationRunStatus.BLOCKED)
        self.assertIn("project does not match", blocked.block_reason)
        self.assertEqual(self.threads.calls, [])

    async def test_always_create_queues_governed_work_item_action_intent(self) -> None:
        run = self._publish_and_admit(work_item_policy="always_create")

        waiting = await self.service.launch(
            run.id,
            organization_id="local",
            workspace_id="default",
            work_item_ref="group/app#42",
        )

        self.assertEqual(
            waiting.status,
            AutomationRunStatus.WAITING_FOR_WORK_ITEM,
        )
        self.assertEqual(
            waiting.work_item_action_intent_id,
            "action-intent-work-item-1",
        )
        self.assertEqual(self.threads.calls, [])
        self.assertEqual(len(self.action_intents.calls), 1)
        payload, actor = self.action_intents.calls[0]
        self.assertEqual(payload.request.action_id, "task-source.create")
        self.assertEqual(payload.request.project_id, "home")
        self.assertEqual(
            payload.request.idempotency_key,
            f"automation:{run.id}:work-item-create",
        )
        self.assertEqual(actor.identity_id, "automation-owner")

    async def test_reuse_or_create_without_existing_work_item_queues_creation(self) -> None:
        run = self._publish_and_admit(work_item_policy="reuse_or_create")

        waiting = await self.service.launch(
            run.id,
            organization_id="local",
            workspace_id="default",
        )

        self.assertEqual(
            waiting.status,
            AutomationRunStatus.WAITING_FOR_WORK_ITEM,
        )
        self.assertEqual(len(self.action_intents.calls), 1)
        self.assertEqual(self.turns.calls, [])

    async def test_successful_work_item_action_intent_resumes_and_launches(self) -> None:
        run = self._publish_and_admit(work_item_policy="always_create")
        await self.service.launch(
            run.id,
            organization_id="local",
            workspace_id="default",
        )
        self.action_intents.histories["action-intent-work-item-1"] = {
            "receipts": [
                {
                    "result": {
                        "status": "succeeded",
                        "output": {"work_item_ref": "group/app#42"},
                    }
                }
            ]
        }

        results = await self.service.resume_for_action_intent(
            SimpleNamespace(
                id="action-intent-work-item-1",
                organization_id="local",
                workspace_id="default",
                status=ActionIntentStatus.SUCCEEDED,
                last_error=None,
            )
        )

        self.assertEqual(len(results), 1)
        resumed = results[0]
        self.assertEqual(resumed.status, AutomationRunStatus.RUNNING)
        self.assertEqual(resumed.work_item_ref, "group/app#42")
        self.assertEqual(len(self.turns.calls), 1)
        self.assertEqual(
            self.turns.calls[0][2]["work_item_ref"],
            "group/app#42",
        )

    async def test_failed_work_item_action_intent_blocks_waiting_run(self) -> None:
        run = self._publish_and_admit(work_item_policy="always_create")
        await self.service.launch(
            run.id,
            organization_id="local",
            workspace_id="default",
        )

        results = await self.service.resume_for_action_intent(
            SimpleNamespace(
                id="action-intent-work-item-1",
                organization_id="local",
                workspace_id="default",
                status=ActionIntentStatus.FAILED,
                last_error="authoritative task creation failed",
            )
        )

        self.assertEqual(len(results), 1)
        blocked = results[0]
        self.assertEqual(blocked.status, AutomationRunStatus.BLOCKED)
        self.assertEqual(
            blocked.block_code,
            "automation_work_item_creation_failed",
        )
        self.assertEqual(self.turns.calls, [])

    async def test_team_target_without_work_item_queues_governed_creation(self) -> None:
        run = self._publish_and_admit(target_kind="team")

        waiting = await self.service.launch(
            run.id,
            organization_id="local",
            workspace_id="default",
        )

        self.assertEqual(
            waiting.status,
            AutomationRunStatus.WAITING_FOR_WORK_ITEM,
        )
        self.assertEqual(
            waiting.work_item_action_intent_id,
            "action-intent-work-item-1",
        )
        self.assertEqual(self.teams.calls, [])
        self.assertEqual(len(self.action_intents.calls), 1)


if __name__ == "__main__":
    unittest.main()
