"""Actual worker/backend ownership cleanup retains facts, never scan acceptance."""
import unittest
from unittest.mock import Mock, patch

from codex_web.local_execution_backend import BubblewrapExecutionBackend
from tests.test_local_execution_worker import _probe_success
from tests.test_skill_scanner_phase_evidence import SkillScannerPhaseEvidenceTests as _Fixture


class SkillScannerInterruptedProcessTests(unittest.TestCase):
    setUp = _Fixture.setUp
    run_failure = _Fixture.run_failure

    def install_backend(self, *, before_spawn=False, wait_error=False):
        process = Mock(pid=99999999)
        process.poll.return_value = None
        process.wait.side_effect = RuntimeError('PRIVATE wait failure') if wait_error else None
        process.wait.return_value = -9
        spawned = []
        def popen(*args, **kwargs):
            spawned.append(True)
            return process
        backend = BubblewrapExecutionBackend(executable='/usr/bin/bwrap',
            probe_runner=_probe_success, popen=popen)
        run = backend.run
        def controlled_run(*args, poll_hook, **kwargs):
            def poll():
                poll_hook()  # Actual current canonical identity/custody checks still execute.
                if before_spawn or spawned:
                    raise RuntimeError('PRIVATE controlled owner refusal')
            return run(*args, poll_hook=poll, **kwargs)
        backend.run = controlled_run
        self.runtime.backend = backend
        return backend, process, spawned

    def test_reaped_process_status_survives_owner_and_completion_failure(self):
        backend, process, spawned = self.install_backend()
        self.workers.complete = Mock(side_effect=RuntimeError('PRIVATE completion unavailable'))
        with patch.object(backend, '_kill_process_group') as kill:
            observed, assignment = self.run_failure()
        self.assertEqual(spawned, [True])
        kill.assert_called_once_with(process)
        process.wait.assert_called_once()
        self.assertEqual(observed['scanner_phase'], 'backend_stopped_after_error')
        self.assertEqual(observed['scanner_exit_code'], -9)
        self.assertGreaterEqual(observed['scanner_duration_seconds'], 0)
        self.assertIsNone(observed['scanner_output_truncated'])
        self.assertFalse(observed['scanner_completion_returned'])
        self.assertEqual(assignment.status, 'running')

    def test_failed_wait_keeps_process_outcome_unknown(self):
        backend, process, spawned = self.install_backend(wait_error=True)
        with patch.object(backend, '_kill_process_group') as kill:
            observed, _ = self.run_failure()
        self.assertEqual(spawned, [True])
        kill.assert_called_once_with(process)
        process.wait.assert_called_once()
        self.assertEqual(observed['scanner_phase'], 'backend_unresolved')
        self.assertIsNone(observed['scanner_exit_code'])
        self.assertIsNone(observed['scanner_duration_seconds'])

    def test_pre_spawn_refusal_does_not_invent_process_or_returncode(self):
        backend, process, spawned = self.install_backend(before_spawn=True)
        with patch.object(backend, '_kill_process_group') as kill:
            observed, _ = self.run_failure()
        self.assertEqual(spawned, [])
        kill.assert_not_called()
        process.wait.assert_not_called()
        self.assertIsNone(observed['scanner_exit_code'])
        self.assertFalse(observed['scanner_result_accepted'])

    def test_observed_exit_zero_after_refusal_is_not_scan_acceptance(self):
        backend, process, _ = self.install_backend()
        process.wait.return_value = 0
        with patch.object(backend, '_kill_process_group'):
            observed, assignment = self.run_failure()
        self.assertEqual(observed['scanner_phase'], 'backend_stopped_after_error')
        self.assertEqual(observed['scanner_exit_code'], 0)
        self.assertEqual(assignment.status, 'failed')
        self.assertFalse(observed['scanner_completion_returned'])
        self.assertFalse(observed['scanner_result_accepted'])

    def test_cancellation_preserves_same_exception_after_join_and_observation(self):
        import asyncio
        from tests.test_local_execution_worker import _assignment
        backend, process, spawned = self.install_backend()
        cancellation = asyncio.CancelledError('PRIVATE cancelled')
        observations = []
        def cancelled_after_spawn():
            if spawned:
                raise cancellation
        with patch.object(backend, '_kill_process_group') as kill:
            with self.assertRaises(asyncio.CancelledError) as caught:
                BubblewrapExecutionBackend.run(backend, _assignment(),
                    argv=('python3', '-c', 'pass'), workspace_path=self.root,
                    poll_hook=cancelled_after_spawn, failure_observer=observations.append)
        self.assertIs(caught.exception, cancellation)
        kill.assert_called_once_with(process)
        process.wait.assert_called_once()
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].exit_code, -9)
        self.assertIsNone(observations[0].output_truncated)

    def test_observer_error_does_not_mask_original_owner_refusal(self):
        from tests.test_local_execution_worker import _assignment
        backend, process, spawned = self.install_backend()
        refusal = RuntimeError('PRIVATE original owner refusal')
        observer = Mock(side_effect=ValueError('PRIVATE observer failure'))
        def refuse_after_spawn():
            if spawned:
                raise refusal
        with patch.object(backend, '_kill_process_group'):
            with self.assertRaises(RuntimeError) as caught:
                BubblewrapExecutionBackend.run(backend, _assignment(),
                    argv=('python3', '-c', 'pass'), workspace_path=self.root,
                    poll_hook=refuse_after_spawn, failure_observer=observer)
        self.assertIs(caught.exception, refusal)
        observer.assert_called_once()
        self.assertEqual(observer.call_args.args[0].exit_code, -9)
        process.wait.assert_called_once()


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(SkillScannerInterruptedProcessTests)
