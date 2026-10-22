"""Real offline READY checkpoints must use their already-proven exact origin."""
import unittest
from unittest.mock import patch

from codex_web.execution_workers import AssignmentStatus
from codex_web.services import code_host_readiness
from tests import test_pull_request_ready as ready


class ReadyOriginPointLookupTests(unittest.IsolatedAsyncioTestCase):
    _assignment = staticmethod(ready.PullRequestReadyTests._assignment)
    _publish_worker_authority = ready.PullRequestReadyTests._publish_worker_authority
    _publish_profile = ready.PullRequestReadyTests._publish_profile
    _request = ready.PullRequestReadyTests._request
    asyncSetUp = ready.PullRequestReadyTests.asyncSetUp
    asyncTearDown = ready.PullRequestReadyTests.asyncTearDown
    change_assignment = ready.PullRequestReadyTests.change_assignment
    submit = ready.PullRequestReadyTests.submit

    def respond(self, request):
        response = ready.PullRequestReadyTests.respond(self, request)
        mutation = getattr(self, 'point_mutation', None)
        if mutation and request.method == 'GET' and request.url.path.endswith('/pulls/7'):
            self.point_mutation = None
            mutation()
        return response

    async def test_repeated_ready_checkpoints_do_not_scan_retained_assignment_catalog(self):
        foreign = [self.assignment.model_copy(update={
            'id': f'assignment-retained-foreign-{i}', 'execution_id': f'foreign-execution-{i}',
            'status': AssignmentStatus.CANCELLED, 'lease': None,
            'failure_message': 'retained harmless fixture ' * 80,
        }) for i in range(32)]
        def append(state):
            state.assignments.extend(type(self.assignment).model_validate(x.model_dump()) for x in foreign)
            return state
        self.workers.store.update(append)
        original = self.workers.store.list_assignments
        checkpoint_scans = []
        def observed():
            if code_host_readiness._current.get() is not None:
                checkpoint_scans.append(True)
            return original()
        with patch.object(self.workers.store, 'list_assignments', side_effect=observed):
            status, _, body = await self.submit('ready-exact-origin-no-catalog')
        self.assertEqual(status, 200, body)
        self.assertEqual(body['item']['status'], 'succeeded', body)
        self.assertEqual(checkpoint_scans, [],
            'Fresh READY checkpoints must point-read the canonical originating row, not decode foreign history')
        history = self.intents.history(body['item']['id'], self.admin)
        self.assertTrue(history['verifications'][-1]['verified'])
        self.assertEqual(sum(method == 'POST' for method, _ in self.calls), 1)

    async def test_current_origin_fence_is_rechecked_before_write(self):
        self.point_mutation = lambda: self.change_assignment(fence=self.assignment.fence + 1)
        _, _, body = await self.submit('ready-point-current-fence')
        self.assertNotEqual(body.get('item', {}).get('status'), 'succeeded', body)
        self.assertFalse(any(method == 'POST' for method, _ in self.calls))

    async def test_current_origin_lease_is_rechecked_before_write(self):
        self.point_mutation = lambda: self.change_assignment(
            lease=self.assignment.lease.model_copy(update={'expires_at': 0}))
        _, _, body = await self.submit('ready-point-current-lease')
        self.assertNotEqual(body.get('item', {}).get('status'), 'succeeded', body)
        self.assertFalse(any(method == 'POST' for method, _ in self.calls))

    async def test_changed_execution_cannot_use_original_assignment(self):
        self.point_mutation = lambda: self.change_assignment(execution_id='another-current-execution')
        _, _, body = await self.submit('ready-point-execution-changed')
        self.assertNotEqual(body.get('item', {}).get('status'), 'succeeded', body)
        self.assertFalse(any(method == 'POST' for method, _ in self.calls))

    async def test_unbound_assignment_resolution_keeps_existing_catalog_path(self):
        with patch.object(self.workers.store, 'list_assignments',
                wraps=self.workers.store.list_assignments) as full:
            current = self.workers.assignment_for_execution(self.assignment.execution_id, actor=self.admin)
        self.assertEqual(current.id, self.assignment.id)
        full.assert_called_once()
