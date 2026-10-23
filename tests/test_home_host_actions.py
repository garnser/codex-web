"""Private contracts and fake-process execution; never run a host command."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from codex_web.execution_profiles import (HOME_HOST_COMMAND_CONTRACT, HomeOperatorEnvelope,
    validate_execution_profile_catalog, validate_administrative_execution_profile_catalog,
    validate_coordination_execution_profile_catalog, validate_home_execution_profile_catalog)
from codex_web.execution_profile_seed import execution_profile_catalog_seed_payload
from codex_web.execution_workers import WorkerResourceLimits
from codex_web.local_execution_backend import LocalExecutionPolicyError
from codex_web.trusted_host_backend import TrustedHostActionBackend


class HomeHostContractTests(unittest.TestCase):
    def envelope(self, **changes):
        return {'host_resource_id':'resource-local-host','trusted_worker_ids':['local-worker'],
            'host_network_access':True, **changes}
    def test_optin_13_and_legacy_rejection(self):
        data=execution_profile_catalog_seed_payload()
        next(p for p in data['profiles'] if p['id']=='orchestration-only')['home_operator']=self.envelope()
        self.assertIn('home_operator', validate_home_execution_profile_catalog(data)['profiles'][1])
        for old in (validate_execution_profile_catalog,validate_administrative_execution_profile_catalog,validate_coordination_execution_profile_catalog):
            with self.subTest(old=old.__name__),self.assertRaisesRegex(ValueError,'1.3'):old(data)
    def test_no_implicit_legacy_serialization(self):
        self.assertTrue(all('home_operator' not in p for p in validate_execution_profile_catalog(execution_profile_catalog_seed_payload())['profiles']))
    def test_network_ack_is_explicit_boolean_true(self):
        for value in (False,1,'true',None):
            with self.subTest(value=value),self.assertRaises(ValueError):HomeOperatorEnvelope.model_validate(self.envelope(host_network_access=value))
    def test_no_implicit_or_duplicate_workers(self):
        for workers in ([],['worker-1','worker-1']):
            with self.assertRaises(ValueError):HomeOperatorEnvelope.model_validate(self.envelope(trusted_worker_ids=workers))
    def test_no_duplicate_target_or_profile_and_strict_revision(self):
        target={'project_id':'p','child_profiles':[{'profile_id':'developer','revision':1}]}
        for targets in ([target,target],[{'project_id':'p','child_profiles':[{'profile_id':'developer','revision':True}]}],
                        [{'project_id':'p','child_profiles':target['child_profiles']*2}]):
            with self.assertRaises(ValueError):HomeOperatorEnvelope.model_validate(self.envelope(project_targets=targets))
    def test_bounds_are_strict(self):
        for changes in ({'max_command_seconds':True},{'max_command_seconds':601},{'max_output_bytes':65537}):
            with self.assertRaises(ValueError):HomeOperatorEnvelope.model_validate(self.envelope(**changes))


class TrustedHostBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.assignment=SimpleNamespace(execution_contract_version=HOME_HOST_COMMAND_CONTRACT,
            assigned_worker_id='local-worker', limits=WorkerResourceLimits(wall_seconds=1))
        self.guard=Mock()
        self.launches=[]
        def popen(argv,**kw):
            self.launches.append((argv,kw));kw['stdout'].write(b'bounded output')
            return SimpleNamespace(poll=lambda:0,wait=lambda:0)
        self.backend=TrustedHostActionBackend(worker_id='local-worker', expected_uid=os.geteuid(),
            enabled=True,popen=popen,max_output_bytes=1024)
    def run_action(self):
        return self.backend.run_action(self.assignment,argv=('/usr/bin/true',),
            workspace_path=Path(self.temp.name),admission_guard=self.guard)
    def test_positive_uses_existing_engine_without_process_or_env_inheritance(self):
        result=self.run_action()
        self.assertTrue(result.succeeded);self.assertEqual(result.stdout,'bounded output')
        self.assertEqual(self.launches[0][0],('/usr/bin/true',))
        self.assertTrue(self.launches[0][1]['start_new_session'])
        self.assertNotIn('HOME',self.launches[0][1]['env'])
        self.assertGreaterEqual(self.guard.call_count,2)
    def test_output_limit_is_per_action_and_restored_without_backend_mutation(self):
        self.backend.max_output_bytes = 65536
        observed = []
        self.guard.side_effect = lambda: observed.append(self.backend.max_output_bytes)
        self.backend.run_action(self.assignment, argv=('/usr/bin/true',),
            workspace_path=Path(self.temp.name), admission_guard=self.guard, max_output_bytes=1024)
        self.assertTrue(observed)
        self.assertEqual(set(observed), {1024})
        self.assertEqual(self.backend.max_output_bytes, 65536)
        for invalid in (True, 0, 65537):
            with self.assertRaises(LocalExecutionPolicyError):
                self.backend.run_action(self.assignment, argv=('/usr/bin/true',),
                    workspace_path=Path(self.temp.name), admission_guard=self.guard, max_output_bytes=invalid)
        self.assertEqual(len(self.launches), 1)

    def test_ordinary_run_and_unadmitted_build_are_refused(self):
        with self.assertRaises(LocalExecutionPolicyError):self.backend.run(self.assignment)
        with self.assertRaises(LocalExecutionPolicyError):self.backend.build_command(self.assignment,argv=('/bin/sh',))
        self.assertEqual(self.launches,[])
    def test_default_disabled_uid_drift_and_contract_drift(self):
        for change in ('disabled','uid','contract'):
            with self.subTest(change=change):
                self.backend.enabled=change!='disabled';self.backend.expected_uid=os.geteuid()+(change=='uid')
                self.assignment.execution_contract_version='1.0' if change=='contract' else HOME_HOST_COMMAND_CONTRACT
                with self.assertRaises(LocalExecutionPolicyError):self.run_action()
        self.assertEqual(self.launches,[])
    def test_fresh_admission_refusal_has_no_launch(self):
        self.guard.side_effect=RuntimeError('authority revoked')
        with self.assertRaisesRegex(RuntimeError,'revoked'):self.run_action()
        self.assertEqual(self.launches,[])
    def test_malformed_argv_has_no_launch(self):
        with self.assertRaises(LocalExecutionPolicyError):
            self.backend.run_action(self.assignment,argv=('relative',),workspace_path=Path(self.temp.name),admission_guard=self.guard)
        self.assertEqual(self.launches,[])
