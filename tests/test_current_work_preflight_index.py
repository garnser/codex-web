"""Custody reconciliation must inspect beyond the operator's bounded page."""
import unittest

from fastapi import HTTPException

from codex_web.models import QueuedTurn
from tests import test_execution_preflight as fixture


class CompleteRecipientPreflightTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ExecutionPreflightServiceTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def retain(self, number, *, organization_id='org'):
        self.f.clock_value += 1  # Canonical retention uses the service clock.
        return self.f.service.retain_queued(QueuedTurn(id='original-' + str(number),
            execution_id='execution-' + str(number), thread_id='same-recipient',
            project_id='project', work_item_ref='team/existing!' + str(number),
            message='Existing retained input', created_at=100 + number),
            organization_id=organization_id, workspace_id='ws')

    def test_complete_view_includes_original_beyond_full_operator_page(self):
        oldest = self.retain(0)
        _, claim = self.f.service.claim_queued_admission(oldest.id)
        self.f.service.begin_queued_start(oldest.id, claim_id=claim)
        # 200 is the existing storage page contract, not a performance target.
        for number in range(1, 201):
            self.retain(number)
        self.assertNotIn(oldest.id, {row.id for row in self.f.store.for_thread('same-recipient', limit=200)})
        complete = self.f.service.for_thread_complete('same-recipient', actor=self.f.admin)
        self.assertEqual(len(complete), 201)
        saved = next(row for row in complete if row.id == oldest.id)
        self.assertEqual(saved.status, 'reconciliation_required')
        self.assertEqual(saved.queued_admission_claim_id, claim)
        self.assertIsNone(saved.queued_start_receipt)

    def test_dangling_index_and_foreign_member_are_not_silently_filtered(self):
        self.retain(0)
        key = 't/same-recipient/0000000000000000/dangling'
        self.f.store.store.record_apply(self.f.store.NAMESPACE,
            upserts={key: {'attemptId': 'missing-original'}})
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            self.f.service.for_thread_complete('same-recipient', actor=self.f.admin)
        self.f.store.store.record_apply(self.f.store.NAMESPACE, upserts={}, deletes=(key,))
        self.retain(1, organization_id='foreign')
        with self.assertRaises(HTTPException) as refused:
            self.f.service.for_thread_complete('same-recipient', actor=self.f.admin)
        self.assertIn(refused.exception.status_code, (403, 404))
