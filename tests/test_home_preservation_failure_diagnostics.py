"""Real Home/Upgrade API preserves incomplete budget/query evidence without retry."""
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from codex_web.api.upgrades import build_upgrades_router
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.storage.state_store import storage_operation_budget, check_operation_budget
from tests.test_home_unowned_upgrade import UnownedHomeUpgradeTests
try:
    from psycopg.errors import QueryCanceled
except ImportError:
    QueryCanceled = None


class HomePreservationFailureDiagnosticTests(UnownedHomeUpgradeTests):
    async def request_drain(self):
        app = FastAPI()
        app.include_router(build_upgrades_router(self.upgrades))
        get_release = self.releases.get
        def typed_release(*args, **kwargs):
            value = get_release(*args, **kwargs)
            return value.model_copy(update={'build': value.build.model_copy(
                update={'source_revision': 'b' * 40})})
        with patch('codex_web.api.upgrades.request_actor', return_value=self.operator), \
                patch.object(self.releases, 'get', side_effect=typed_release):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,
                    raise_app_exceptions=False), base_url='http://private-fixture') as client:
                return await client.post('/api/upgrades/' + self.plan.id + '/service-drain',
                    json={'service_instance_id': 'fixture-service',
                          'source_revision': 'a' * 40, 'target_revision': 'b' * 40})

    def incomplete(self, response, phase, exception_type):
        self.assertEqual(response.status_code, 200)
        self.assertIn('application/json', response.headers.get('content-type', ''))
        detail = response.json()['item']['home_checkpoint_disposition']
        self.assertEqual(detail['reason_code'], 'home_upgrade_preservation_incomplete')
        self.assertEqual((detail['phase'], detail['exception_type']), (phase, exception_type))
        self.assertEqual(detail['outcome'], 'deferred_unknown')
        self.assertIs(detail['continuation_authorized'], False)
        self.assertNotIn('PRIVATE_SECRET', response.text)
        current = self.upgrades.get(self.plan.id, actor=self.operator)
        self.assertTrue(current.maintenance_mode)
        self.assertEqual(current.admission_epoch, 1)
        self.assertEqual(self.effects, 0)

    async def test_real_inner_budget_expiry_is_typed_at_the_original_api(self):
        self.lose()
        rows = self.f.sqlite.home_intent_recovery_rows
        calls = []
        def expire(*args, **kwargs):
            result = rows(*args, **kwargs)
            calls.append(1)
            budget = storage_operation_budget.get()
            self.assertIsNotNone(budget)
            budget.deadline -= 6
            check_operation_budget()
            return result
        with patch.object(self.f.sqlite, 'home_intent_recovery_rows', side_effect=expire):
            response = await self.request_drain()
        self.incomplete(response, 'initial_snapshot', 'TimeoutError')
        self.assertEqual(calls, [1])
        self.assertIsNone(self.planned._dormant_artifact(self.plan.id, self.actor))

    @unittest.skipIf(QueryCanceled is None, 'PostgreSQL dependency absent; mandatory distributed lane runs this case')
    async def test_real_postgres_cancellation_class_has_no_sql_or_error_text(self):
        self.lose()
        with patch.object(self.f.sqlite, 'home_intent_recovery_rows',
                side_effect=QueryCanceled('PRIVATE_SECRET SQL and parameter content')) as query:
            response = await self.request_drain()
        self.incomplete(response, 'initial_snapshot', 'QueryCanceled')
        self.assertEqual(query.call_count, 1)

    async def test_final_expiry_retains_real_artifact_as_unknown_not_no_write(self):
        self.lose()
        create = self.artifacts.create_artifact
        created = []
        def expire_after_real_commit(*args, **kwargs):
            artifact = create(*args, **kwargs)
            created.append(artifact.id)
            storage_operation_budget.get().deadline -= 6
            return artifact
        with patch.object(self.artifacts, 'create_artifact', side_effect=expire_after_real_commit):
            response = await self.request_drain()
        self.incomplete(response, 'final_snapshot', 'TimeoutError')
        self.assertEqual(len(created), 1)
        self.assertEqual(self.planned._dormant_artifact(self.plan.id, self.actor).id, created[0])
        self.assertNotIn('artifact_absent', response.json()['item']['home_checkpoint_disposition'])

    async def test_unrelated_programming_error_is_not_classified(self):
        self.lose()
        with patch.object(self.f.sqlite, 'home_intent_recovery_rows',
                side_effect=TypeError('PRIVATE_SECRET unrelated defect')):
            response = await self.request_drain()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.text, 'Internal Server Error')

    async def test_existing_authority_refusal_keeps_its_original_status_and_detail(self):
        self.lose()
        detail = {'code': 'existing_authority_denied', 'retryable': False}
        with patch.object(self.planned, '_dormant_snapshot',
                side_effect=ExecutionPrestartRefusal(status_code=409, detail=detail)):
            response = await self.request_drain()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {'detail': detail})

    async def test_nonhome_timeout_is_not_relabelled_as_home_preservation(self):
        with patch.object(self.upgrades, 'start_service_drain',
                side_effect=TimeoutError('PRIVATE_SECRET outer service error')):
            response = await self.request_drain()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.text, 'Internal Server Error')


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(loader.loadTestsFromName(name, HomePreservationFailureDiagnosticTests)
        for name in HomePreservationFailureDiagnosticTests.__dict__ if name.startswith('test_'))
