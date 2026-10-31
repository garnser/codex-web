"""#1913: attempts are audit records, not evidence of recipient activity."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from codex_web.services.bot_event_dispatch import BotEventDispatchService
from codex_web.services.bot_runtime_telemetry import BotRuntimeTelemetry
from tests import test_watchdog_recovery_admission as fixture_module


NOOPS = (
    {'ok': True, 'ignored': True, 'reason': 'canonical_duplicate'},
    {'ok': False, 'ignored': True, 'reason': 'autonomy_skipped',
     'autonomyReason': 'reasoning_cooldown_active'},
    {'ok': True, 'ignored': True, 'reason': 'owner_lane_busy'},
    {'ok': False, 'reason': 'owner_routing_blocked',
     'code': 'owner_recipient_native_custody_unverified'},
    {'ok': True, 'queued': False, 'skipped': 'already_active'},
)


class WatchdogNoopActivityTests(unittest.IsolatedAsyncioTestCase):
    def telemetry(self, directory):
        return BotRuntimeTelemetry(events_file=Path(directory) / 'bot-events.jsonl')

    def recent_activity(self, telemetry):
        # Real existing grace projection, not a test reimplementation.
        return BotEventDispatchService(projects=None, settings=None, targets=None, bindings=None,
            execution=None, queue_policy=None, recovery=None, resume=None, telemetry=telemetry,
            publish_event=AsyncMock(), binding_name=lambda binding: binding.thread_id)

    async def test_skipped_watchdog_does_not_hide_pending_handoff_retry(self):
        for result in NOOPS:
            with self.subTest(result=result), tempfile.TemporaryDirectory() as directory:
                telemetry = self.telemetry(directory)
                telemetry.append({'type': 'owner_work_watchdog_dispatched',
                    'thread_id': 'predecessor', 'result': result})
                fixtures = fixture_module.WatchdogRecoveryAdmissionTests()
                runtime = fixtures.runtime(handoff=True)
                runtime.thread_recently_active = self.recent_activity(telemetry).thread_recently_active
                pin = SimpleNamespace(recipient=SimpleNamespace(thread_id='successor'))
                runtime.owner_routing = SimpleNamespace(
                    prepare_compatible_pin=AsyncMock(return_value=pin), record_failure=Mock())
                service = fixtures.service(runtime, directory)
                nonce = runtime.state.handoff.requested_at
                await service.run_work_item_sla_cycle()
                runtime.owner_routing.prepare_compatible_pin.assert_awaited_once()
                runtime.dispatch_event.assert_awaited_once()
                kwargs = runtime.dispatch_event.await_args.kwargs
                self.assertIs(kwargs['owner_routing_pin'], pin)
                self.assertEqual(kwargs['handoff_requested_at'], nonce)
                self.assertTrue(kwargs['require_idle'])
                runtime.replace_nonperforming_thread.assert_not_awaited()
                # A successful dispatch call is still not a fabricated START/ACK.
                self.assertEqual(runtime.state.handoff.requested_at, nonce)
                self.assertIsNone(runtime.state.handoff.acknowledged_at)

    async def test_actual_native_activity_still_suppresses_duplicate_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            telemetry = self.telemetry(directory)
            telemetry.append({'type': 'turn_started', 'thread_id': 'predecessor'})
            fixtures = fixture_module.WatchdogRecoveryAdmissionTests()
            runtime = fixtures.runtime(handoff=True)
            runtime.thread_recently_active = self.recent_activity(telemetry).thread_recently_active
            runtime.owner_routing = SimpleNamespace(prepare_compatible_pin=AsyncMock(), record_failure=Mock())
            service = fixtures.service(runtime, directory)
            await service.run_work_item_sla_cycle()
            runtime.owner_routing.prepare_compatible_pin.assert_not_awaited()
            runtime.dispatch_event.assert_not_awaited()

    async def test_noop_filter_cannot_bypass_canonical_autonomy_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            telemetry = self.telemetry(directory)
            telemetry.append({'type': 'owner_work_watchdog_dispatched', 'thread_id': 'predecessor',
                'result': {'ok': True, 'reason': 'canonical_duplicate'}})
            fixtures = fixture_module.WatchdogRecoveryAdmissionTests()
            runtime = fixtures.runtime(handoff=True)
            runtime.thread_recently_active = self.recent_activity(telemetry).thread_recently_active
            runtime.owner_routing = SimpleNamespace(prepare_compatible_pin=AsyncMock(), record_failure=Mock())
            service = fixtures.service(runtime, directory, denial='paused')
            await service.run_work_item_sla_cycle()
            runtime.owner_routing.prepare_compatible_pin.assert_not_awaited()
            runtime.dispatch_event.assert_not_awaited()

    def test_noops_preserve_old_native_activity_raw_journal_and_event_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            telemetry = self.telemetry(directory)
            telemetry.append({'type': 'turn_started', 'thread_id': 'thread', 'created_at': 400.0})
            for index, result in enumerate(NOOPS):
                telemetry.append({'type': 'owner_work_watchdog_dispatched', 'thread_id': 'thread',
                    'created_at': 990.0 + index, 'result': result})
            before = telemetry.events_file.read_bytes()
            self.assertEqual(telemetry.thread_recent_activity_age_seconds('thread', now=1000), 600.0)
            self.assertEqual(telemetry.thread_recent_event_count('thread', {'owner_work_watchdog_dispatched'},
                now=1000, within_seconds=30), len(NOOPS))
            self.assertEqual(telemetry.events_file.read_bytes(), before)

    def test_successful_queue_and_actual_turn_remain_activity(self):
        for event in (
            {'type': 'owner_work_watchdog_dispatched', 'result': {'ok': True, 'queued': True, 'queuedId': 'q'}},
            {'type': 'release_gate_watchdog_dispatched', 'result': {'turn': {'id': 'actual-turn'}}},
            {'type': 'turn_started', 'result': {'ok': False}},
        ):
            with self.subTest(event=event), tempfile.TemporaryDirectory() as directory:
                telemetry = self.telemetry(directory)
                telemetry.append({**event, 'thread_id': 'thread', 'created_at': 990.0})
                self.assertEqual(telemetry.thread_recent_activity_age_seconds('thread', now=1000), 10.0)

    def test_legacy_unknown_outcome_and_foreign_thread_records_preserve_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            telemetry = self.telemetry(directory)
            telemetry.append({'type': 'owner_work_watchdog_dispatched', 'thread_id': 'thread', 'created_at': 990.0})
            telemetry.append({'type': 'owner_work_watchdog_dispatched', 'thread_id': 'foreign',
                'created_at': 999.0, 'result': {'ok': False}})
            self.assertEqual(telemetry.thread_recent_activity_age_seconds('thread', now=1000), 10.0)
            self.assertIsNone(telemetry.thread_recent_activity_age_seconds('foreign', now=1000))

    def test_unknown_structured_reason_cannot_break_or_reclassify_activity(self):
        for reason in ([], {}, ['canonical_duplicate'], {'code': 'canonical_duplicate'}, None):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                telemetry = self.telemetry(directory)
                telemetry.append({'type': 'owner_work_watchdog_dispatched', 'thread_id': 'thread',
                    'created_at': 990.0, 'result': {'reason': reason}})
                self.assertEqual(telemetry.thread_recent_activity_age_seconds('thread', now=1000), 10.0)
