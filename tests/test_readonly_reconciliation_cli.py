"""Operator one-shot sequencing, credential privacy and failure cleanup."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch


class ReadOnlyReconciliationCliTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('readonly_cli',
            Path(__file__).resolve().parents[1]/'scripts/reconcile-readiness-once.py')
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.args = ['--intent-id','intent','--assignment-id','assignment','--worker-id','worker',
            '--worker-service-identity','service','--expected-runtime-revision','installed',
            '--expected-tool-revision','tool','--gitlab-api-base','https://gitlab.test/api/v4','--gitlab-provider-instance','gitlab.test',
            '--fence','1','--expected-runtime-pid','123', '--lock-file',str(self.path/'lock'),
            '--journal-directory',str(self.path/'journal'), '--secret-material-directory',str(self.path/'secrets'),
            '--projects-file',str(self.path/'projects'), '--sqlite-path',str(self.path/'state'), '--execute']
        self.events = []
        self.fail_guard = False
        self.fail_reconcile = False
        self.fail_revoke = False

    def api(self, _base, method, path, payload=None):
        self.events.append((method,path))
        if path == '/api/version':
            return {'gitRevision':'installed'}
        if method == 'POST':
            self.assertEqual(payload['service_identity_id'], 'service')
            self.assertEqual(payload['scopes'], ['action-intent:worker','secret:use'])
            return {'token_id':'issued-token', 'token':'NEVER-LOG-THIS', 'expires_at':99999999999}
        if self.fail_revoke:
            raise TimeoutError('NEVER-LOG-THIS')
        return {'ok':True}

    def guard(self, *args, **kwargs):
        self.events.append(('GUARD','target'))
        if self.fail_guard:
            raise ValueError('unsupported target')

    async def reconcile(self):
        self.events.append(('RECONCILE','intent'))
        if self.fail_reconcile:
            raise TimeoutError('NEVER-LOG-THIS')
        return SimpleNamespace(id='intent', status='succeeded', last_receipt_id='receipt')

    def run_cli(self):
        intent = SimpleNamespace(id='intent', organization_id='org', workspace_id='ws',
            readiness_origin=SimpleNamespace(worker_id='worker'))
        worker = SimpleNamespace(id='worker', service_identity_id='service')
        with patch.object(self.cli.resource,'setrlimit'), patch.object(self.cli.signal,'signal'), \
                patch.object(self.cli.signal,'alarm'), patch.object(self.cli.os,'kill'), \
                patch('codex_web.local_deployment.LOCAL_DEPLOY_LOCK',self.path/'deploy-lock'), \
                patch.object(self.cli.subprocess,'check_output',side_effect=['123\n','tool\n','','tree\n']), \
                patch.object(self.cli,'local_api',side_effect=self.api), \
                patch('codex_web.storage.state_store.build_state_store',return_value=object()), \
                patch('codex_web.storage.action_intents.ActionIntentStore.get',return_value=intent), \
                patch('codex_web.storage.execution_workers.ExecutionWorkerStore.worker',return_value=worker), \
                patch('codex_web.readonly_reconciliation_runtime.validate_readonly_reconciliation_target',side_effect=self.guard), \
                patch('codex_web.readonly_reconciliation_runtime.build_readonly_reconciliation_runtime',
                    return_value=SimpleNamespace(reconcile_once=self.reconcile)):
            return self.cli.main(self.args)

    def test_guard_precedes_credential_and_single_reconcile_finally_revokes(self):
        self.assertEqual(self.run_cli(),0)
        self.assertEqual(self.events, [('GUARD','target'), ('GET','/api/version'),
            ('POST','/api/identity/service-tokens'), ('RECONCILE','intent'),
            ('DELETE','/api/identity/service-tokens/issued-token')])
        self.assertNotIn('NEVER-LOG-THIS', ''.join(path.read_text() for path in (self.path/'journal').iterdir()))
        self.assertTrue((self.path/'journal'/'credential-revoked.json').exists())

    def test_unsupported_target_refuses_before_token_or_other_api(self):
        self.fail_guard = True
        self.assertEqual(self.run_cli(),2)
        self.assertEqual(self.events,[('GUARD','target')])
        self.assertFalse((self.path/'journal'/'credential-creation-attempt.json').exists())

    def test_unknown_reconciliation_is_not_retried_and_credential_revoked(self):
        self.fail_reconcile = True
        self.assertEqual(self.run_cli(),2)
        self.assertEqual(sum(kind=='RECONCILE' for kind,_ in self.events),1)
        self.assertEqual(self.events[-1],('DELETE','/api/identity/service-tokens/issued-token'))
        stopped=json.loads((self.path/'journal'/'stopped.json').read_text())
        self.assertFalse(stopped['retry_authorized'])
        self.assertTrue(stopped['outcome_requires_inspection'])
        self.assertNotIn('NEVER-LOG-THIS', ''.join(path.read_text() for path in (self.path/'journal').iterdir()))

    def test_unconfirmed_revocation_is_failure_even_after_settlement(self):
        self.fail_revoke = True
        self.assertEqual(self.run_cli(),3)
        self.assertTrue((self.path/'journal'/'credential-revocation-unconfirmed.json').exists())

    def test_identity_endpoint_cannot_send_credential_off_host(self):
        for base in ('https://remote.test','http://remote.test','http://user:password@127.0.0.1'):
            with self.subTest(base=base), self.assertRaises(ValueError):
                self.cli.local_api(base,'POST','/api/identity/service-tokens',{})
