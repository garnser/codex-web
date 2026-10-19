"""Real executor/process custody with explicitly controlled deadline observation."""
import asyncio
import subprocess
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from codex_web.execution_workers import WorkerLifecycle
from codex_web.local_execution_backend import BubblewrapExecutionBackend
from codex_web.services.codex_auth_delegation import CodexAuthDelegationStaleError
from codex_web.services import agent_process_session
from codex_web.storage import state_store
from tests import test_runtime_supervision_custody as custody


class ObservationClock:
    """Advance test deadlines at barriers, independently of runner scheduling.

    asyncio and executor futures remain real. The real two-second wait below
    is a fixture deadlock guard, not a claim about production performance.
    """
    def __init__(self):
        self.now = 0.0
        self.loop = asyncio.get_running_loop()
        self.changed = asyncio.Event()
        self.lock = threading.Lock()

    def monotonic(self):
        with self.lock:
            return self.now

    def advance(self, seconds):
        with self.lock:
            self.now += seconds
        self.loop.call_soon_threadsafe(self.changed.set)

    async def wait_for(self, awaitable, seconds):
        deadline = self.monotonic() + seconds
        future = asyncio.ensure_future(awaitable)
        while True:
            if self.monotonic() >= deadline:
                if future.done() and not future.cancelled():
                    future.exception()  # The owned executor future retains the actual outcome.
                else:
                    future.cancel()
                raise TimeoutError('controlled observation deadline')
            if future.done():
                return future.result()
            tick = asyncio.create_task(self.changed.wait())
            try:
                done, _ = await asyncio.wait((future, tick), timeout=2,
                    return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    raise AssertionError('fixture work did not complete or reach its deadline barrier')
            finally:
                tick.cancel()
                await asyncio.gather(tick, return_exceptions=True)
                self.changed.clear()


class SupervisionObservationDeadlineTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the canonical SQLite/worker/session fixture, not its test methods.
    _create_assignment = custody.SupervisionCustodyTests._create_assignment
    _session = custody.SupervisionCustodyTests._session
    asyncSetUp = custody.SupervisionCustodyTests.asyncSetUp
    asyncTearDown = custody.SupervisionCustodyTests.asyncTearDown
    session = custody.SupervisionCustodyTests.session
    observations = custody.SupervisionCustodyTests.observations

    async def exercise(self, *, phase="validation", fault=None, worker_delay=0):
        session, _ = await self.session()
        clock = ObservationClock()
        # Patch only these two consumers, never asyncio's own real clock/timers.
        controlled_time = SimpleNamespace(**{**vars(time), 'monotonic': clock.monotonic})
        controlled_asyncio = SimpleNamespace(**{**vars(asyncio), 'wait_for': clock.wait_for})
        patches = (patch.object(agent_process_session, 'time', controlled_time),
            patch.object(agent_process_session, 'asyncio', controlled_asyncio),
            patch.object(state_store, 'time', controlled_time))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        # All process stop/kill/wait behavior below is real, without a provider.
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                   start_new_session=True)
        def reap():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
        self.addCleanup(reap)
        session.runtime.proc = session._owned_process = process
        self.backend.terminate_process = BubblewrapExecutionBackend._kill_process_group
        session._clock = time.time
        start = asyncio.Event()
        cycle = asyncio.Event()
        hold = asyncio.Event()
        sleeps = 0

        async def sleep(_):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 1:
                await start.wait()
            else:
                cycle.set()
                await hold.wait()
        session._sleep = sleep
        original_budget = session._lease_budget
        session._lease_budget = lambda: min(.15, original_budget())
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        authority_completed = threading.Event()
        finish_unfinished = threading.Event()
        captured = []
        captured_phases = []
        selected = []
        before_observation = []
        submit = session._supervision_executor.submit
        self.worker_service.heartbeat = Mock(wraps=self.worker_service.heartbeat)
        self.worker_service.renew = Mock(wraps=self.worker_service.renew)

        def capture(function, *args, **kwargs):
            future = submit(function, *args, **kwargs)
            captured.append(future)
            captured_phases.append(session._supervision_phase)
            if session._supervision_phase == phase:
                selected.append(future)
                future.add_done_callback(lambda _: completed.set())
            if session._supervision_phase == "renewal_authority":
                future.add_done_callback(lambda _: authority_completed.set())
            return future
        session._supervision_executor.submit = capture
        name = "validate_current" if phase == "validation" else "_heartbeat_and_renew"
        function = getattr(session, name)

        def work(*args):
            entered.set()
            if not release.wait(2):
                raise AssertionError("fixture failed to release bounded work")
            if worker_delay:
                time.sleep(worker_delay)
            if fault == "unfinished":
                # Remain genuinely running while watchdog retires the process.
                # Cancellation must keep custody and refuse any later work.
                from codex_web.storage.state_store import check_operation_budget
                if not finish_unfinished.wait(2):
                    raise AssertionError("unfinished fixture was not reaped")
                check_operation_budget()
            if fault == "over_budget":
                clock.advance(.18)
            if fault == "worker_failure":
                raise RuntimeError("actual worker validation failed")
            return function(*args)
        setattr(session, name, Mock(side_effect=work))
        renewal = session._heartbeat_and_renew
        if phase != "renewal":
            session._heartbeat_and_renew = Mock(wraps=renewal)

        def delay_loop():
            # Deliberately prevent wrap_future callbacks/timers from being observed.
            # Wait on the *same actual submitted future*, not a fabricated DTO.
            release.set()
            if fault == "unfinished":
                clock.advance(.17)
            else:
                self.assertTrue(completed.wait(2))
            before_observation.append(session._bound_assignment.model_dump(mode="json"))
            if fault == "authority":
                self.delegation.validation_error = CodexAuthDelegationStaleError("revoked credential")
            if fault == "authority_gate_unfinished":
                authority = session._validate_current_authority
                def hold_authority():
                    loop.call_soon_threadsafe(clock.advance, .2)
                    if not finish_unfinished.wait(2):
                        raise AssertionError("authority gate fixture was not reaped")
                    return authority()
                # Install only after the real renewal has already completed.
                session._validate_current_authority = hold_authority
            if fault == "authority_gate_late":
                authority = session._validate_current_authority
                def delay_authority_observation():
                    self.assertTrue(authority_completed.wait(2))
                    clock.advance(.2)
                def timely_authority():
                    result = authority()
                    loop.call_soon_threadsafe(delay_authority_observation)
                    return result
                session._validate_current_authority = timely_authority
            if fault == "quarantine":
                self.worker_service.set_lifecycle(self.worker.id, WorkerLifecycle.QUARANTINED,
                                                 actor=self.admin, reason="fixture revocation")
            if fault in {"lease", "deadline", "fence"}:
                def change(state):
                    for index, item in enumerate(state.assignments):
                        if item.id == session.assignment_id:
                            values = ({"fence": item.fence + 1} if fault == "fence" else
                                      {"deadline_at": time.time() - 1} if fault == "deadline" else
                                      {"lease": item.lease.model_copy(update={"expires_at": time.time() - 1})})
                            state.assignments[index] = item.model_copy(update=values)
                    return state
                self.worker_service.store.update(change)
            clock.advance(.2)

        try:
            start.set()
            while not entered.is_set():
                await asyncio.sleep(.001)
            loop = asyncio.get_running_loop()
            loop.call_soon(delay_loop)
            for _ in range(300):
                if session.watchdog_task.done() or cycle.is_set():
                    break
                await asyncio.sleep(.005)
            self.assertTrue(session.watchdog_task.done() or cycle.is_set())
            self.assertEqual(len(selected), 1)
            future = selected[0]
            if fault == "unfinished":
                self.assertTrue(future.running())
                self.assertIn(future, session._executor_work)
            else:
                self.assertTrue(future.done())
            self.assertEqual(getattr(session, name).call_count, 1, "never rerun timed-out work")
            if fault is None:
                self.assertIsNone(future.exception())
                self.assertFalse(session.watchdog_task.done(), session.last_error)
                self.assertIsNone(process.poll(), "successful bounded work must not kill native owner")
                self.assertEqual(self.observations(), [])
                self.assertEqual(captured_phases.count("validation"), 1)
                self.assertEqual(captured_phases.count("renewal"), 1)
                self.assertLessEqual(len(captured), 3, "at most one bounded authority reconciliation")
                self.assertIsNotNone(session._bound_assignment.lease.renewed_at)
            else:
                self.assertTrue(session.watchdog_task.done())
                await session.watchdog_task
                self.assertIsNotNone(process.poll(), "real owned process must be reaped on failure")
                self.assertTrue(session._failure_observation)
                self.assertEqual(session._failure_observation["outcome"], "unknown")
                self.assertEqual(session._failure_observation["cleanup_state"], "stopped")
                if fault in {"over_budget", "worker_failure"}:
                    self.assertIsNotNone(future.exception())
                if fault in {"authority", "quarantine", "lease", "deadline", "fence"}:
                    self.assertEqual(session._failure_observation["code"], "agent_runtime_validation_failed")
                    self.assertFalse(cycle.is_set(), "reject before another watchdog sleep")
                    self.assertEqual(session._bound_assignment.model_dump(mode="json"),
                                     before_observation[0], "never install a stale or changed claim")
                    self.assertEqual(self.worker_service.renew.call_count,
                                     1 if phase == "renewal" else 0)
                    self.assertEqual(self.worker_service.heartbeat.call_count, 0)
                if fault == "authority":
                    self.assertIn("revoked credential", session.last_error)
                if fault in {"authority_gate_unfinished", "authority_gate_late"}:
                    self.assertFalse(cycle.is_set())
                    self.assertEqual(captured_phases, ["validation", "renewal", "renewal_authority"])
                    if fault == "authority_gate_unfinished":
                        self.assertTrue(captured[-1].running())
                        self.assertIn(captured[-1], session._executor_work)
                    else:
                        self.assertTrue(captured[-1].done())
                        self.assertIsNone(captured[-1].exception())
                    self.assertEqual(session._failure_observation["code"], "execution_timeout")
                    self.assertEqual(self.worker_service.renew.call_count, 1)
                    self.assertEqual(self.worker_service.heartbeat.call_count, 0)
        finally:
            release.set()
            finish_unfinished.set()
            await session.stop()
            pending = list(session._executor_work)
            if pending:
                await asyncio.wait_for(asyncio.gather(
                    *[asyncio.wrap_future(work) for work in pending], return_exceptions=True), 2)
            process.wait(timeout=2)
            for item in reversed(patches):
                item.stop()
        self.assertFalse(session._executor_work)

    async def test_completed_validation_observed_after_deadline_keeps_owner(self):
        await self.exercise()

    async def test_completed_renewal_observed_after_deadline_is_not_repeated(self):
        await self.exercise(phase="renewal")

    async def test_actual_unfinished_work_times_out_and_cleans_process(self):
        await self.exercise(fault="unfinished")

    async def test_actual_over_budget_completion_still_fails(self):
        await self.exercise(fault="over_budget")

    async def test_actual_failed_worker_still_fails(self):
        await self.exercise(fault="worker_failure")

    async def test_late_validation_does_not_borrow_expired_lease(self):
        await self.exercise(fault="lease")

    async def test_late_validation_does_not_ignore_expired_deadline(self):
        await self.exercise(fault="deadline")

    async def test_late_validation_does_not_borrow_new_fence(self):
        await self.exercise(fault="fence")

    async def test_late_validation_rechecks_credential_authority_before_renewal(self):
        await self.exercise(fault="authority")

    async def test_late_validation_rechecks_worker_authority_before_renewal(self):
        await self.exercise(fault="quarantine")

    async def test_late_renewal_rechecks_credential_authority_before_sleep(self):
        await self.exercise(phase="renewal", fault="authority")

    async def test_late_renewal_rechecks_worker_authority_before_sleep(self):
        await self.exercise(phase="renewal", fault="quarantine")

    async def test_late_renewal_does_not_borrow_expired_lease(self):
        await self.exercise(phase="renewal", fault="lease")

    async def test_late_renewal_does_not_ignore_expired_deadline(self):
        await self.exercise(phase="renewal", fault="deadline")

    async def test_late_renewal_does_not_borrow_new_fence(self):
        await self.exercise(phase="renewal", fault="fence")

    async def test_late_renewal_authority_gate_is_bounded_and_not_recursively_reconciled(self):
        await self.exercise(phase="renewal", fault="authority_gate_unfinished")

    async def test_late_observation_of_successful_authority_gate_fails_without_recursion(self):
        await self.exercise(phase="renewal", fault="authority_gate_late")

    async def test_slow_runner_still_keeps_timely_renewal_alive(self):
        # Exceed the old 150ms real-time budget, then exercise both dispositions.
        # Timely work stays alive; the deliberately unfinished authority gate
        # still stops its real owned process without repeating renewal.
        await self.exercise(phase='renewal', worker_delay=.25)

    async def test_slow_runner_still_reaches_deliberately_unfinished_authority_gate(self):
        await self.exercise(phase='renewal', fault='authority_gate_unfinished', worker_delay=.25)
