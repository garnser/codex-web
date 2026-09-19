from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.canonical_events import CanonicalEventType
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.provider_capacity import (
    ProviderCapacityReport,
    ProviderCapacityStatus,
    ProviderCapacityWaitCreate,
    ProviderCapacityWaitStatus,
)
from codex_web.services.provider_capacity import ProviderCapacityService
from codex_web.storage.provider_capacity import ProviderCapacityStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Scheduler:
    def __init__(self) -> None:
        self.created = []
        self.cancelled = []

    def create(self, payload, *, actor_id):
        self.created.append((payload, actor_id))
        return SimpleNamespace(id=f"schedule-{len(self.created)}")

    def cancel(self, schedule_id, *, actor_id):
        self.cancelled.append((schedule_id, actor_id))


class _RateLimit(Exception):
    status_code = 429

    def __init__(self, message: str, *, headers=None):
        super().__init__(message)
        self.response = SimpleNamespace(
            status_code=429,
            headers=headers or {},
        )


class ProviderCapacityServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1_900_000_000.0
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.scheduler = _Scheduler()
        self.service = ProviderCapacityService(
            ProviderCapacityStore(sqlite),
            scheduler=self.scheduler,
            clock=lambda: self.now,
            min_probe_interval_seconds=30,
            default_retry_seconds=300,
        )
        self.actor = AuthenticationActor(
            identity_id="admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_codex_snapshot_detects_depletion_and_reset_then_reenables(self) -> None:
        reset_at = self.now + 120
        record = self.service.report_codex_snapshot(
            {
                "ordinaryUsageAllowed": False,
                "rateLimits": {
                    "rateLimitReachedType": "primary",
                    "primary": {
                        "usedPercent": 100,
                        "resetsAt": reset_at,
                    },
                    "secondary": {
                        "usedPercent": 15,
                        "resetsAt": self.now + 3600,
                    },
                },
            },
            actor=self.actor,
        )

        self.assertEqual(record.status, ProviderCapacityStatus.DEPLETED)
        self.assertEqual(record.retry_at, reset_at)
        self.assertTrue(
            self.service.blocking_record(
                "openai",
                "codex",
                actor=self.actor,
            )
        )

        self.now = reset_at + 1
        refreshed = self.service.get("openai", "codex", actor=self.actor)
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.status, ProviderCapacityStatus.AVAILABLE)
        self.assertIsNone(refreshed.retry_at)

    async def test_quota_and_rate_limit_are_classified_separately(self) -> None:
        throttled = self.service.report_exception(
            "openai",
            None,
            _RateLimit("rate limit exceeded", headers={"retry-after": "30"}),
            actor=self.actor,
            source="test",
        )
        self.assertIsNotNone(throttled)
        self.assertEqual(throttled.status, ProviderCapacityStatus.THROTTLED)
        self.assertEqual(throttled.retry_at, self.now + 30)

        depleted = self.service.report_exception(
            "anthropic",
            None,
            _RateLimit(
                "usage limit reached: quota exhausted",
                headers={"retry-after": "120"},
            ),
            actor=self.actor,
            source="test",
        )
        self.assertIsNotNone(depleted)
        self.assertEqual(depleted.status, ProviderCapacityStatus.DEPLETED)
        self.assertEqual(depleted.retry_at, self.now + 120)

    async def test_wait_is_durable_and_scheduler_resume_invokes_handler(self) -> None:
        calls = []
        self.service.register_resume_handler(lambda wait: calls.append(wait.id))
        wait = self.service.wait_for_capacity(
            ProviderCapacityWaitCreate(
                thread_id="thread-a",
                execution_id="exec-a",
                provider_keys=("openai/codex",),
                retry_at=self.now + 60,
                reason="Codex primary window exhausted",
            ),
            actor=self.actor,
        )

        self.assertEqual(wait.status, ProviderCapacityWaitStatus.WAITING)
        self.assertEqual(wait.schedule_id, "schedule-1")
        self.assertEqual(len(self.scheduler.created), 1)
        payload, actor_id = self.scheduler.created[0]
        self.assertEqual(payload.trigger_type, self.service.RESUME_TRIGGER_TYPE)
        self.assertEqual(payload.due_at, self.now + 60)
        self.assertEqual(actor_id, self.actor.identity_id)

        self.now += 60
        await self.service.handle_schedule_event(
            SimpleNamespace(
                event_type=CanonicalEventType.SCHEDULE.value,
                payload={
                    "trigger_type": self.service.RESUME_TRIGGER_TYPE,
                    "payload": {"capacity_wait_id": wait.id},
                },
            )
        )

        stored = next(
            item
            for item in self.service.list_waits(self.actor)
            if item.id == wait.id
        )
        self.assertEqual(stored.status, ProviderCapacityWaitStatus.RESUMED)
        self.assertEqual(calls, [wait.id])

    async def test_probe_refreshes_expired_codex_capacity_before_routing(self) -> None:
        reset_at = self.now + 10
        self.service.report(
            ProviderCapacityReport(
                provider_id="openai",
                runtime_id="codex",
                status=ProviderCapacityStatus.DEPLETED,
                retry_at=reset_at,
                reason="exhausted",
                source="test",
                observed_at=self.now,
            ),
            actor=self.actor,
        )
        probe_calls = []

        async def probe():
            probe_calls.append(True)
            return {
                "ordinaryUsageAllowed": True,
                "rateLimits": {
                    "primary": {"usedPercent": 0, "resetsAt": self.now + 100},
                    "secondary": {"usedPercent": 0, "resetsAt": self.now + 100},
                },
            }

        self.service.register_probe("openai", "codex", probe)
        self.now = reset_at + 1
        refreshed = await self.service.refresh_if_due(
            "openai",
            "codex",
            actor=self.actor,
        )
        self.assertEqual(probe_calls, [True])
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.status, ProviderCapacityStatus.AVAILABLE)


if __name__ == "__main__":
    unittest.main()
