"""Assignment-bound metadata inspection never executes repository helpers on the host."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import time
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.local_execution_backend import BubblewrapExecutionBackend, LocalExecutionPolicyError, LocalExecutionResult
from tests import test_local_execution_worker as worker_fixtures


class IsolatedGitMetadataProtocolTests(unittest.TestCase):
    def test_restricts_existing_assignment_and_never_inherits_git_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / '.git').mkdir()
            assignment = worker_fixtures._assignment()
            original_assignment = assignment.model_dump(mode='json')
            backend = BubblewrapExecutionBackend()
            result = LocalExecutionResult(executable='python3', command_digest='fixture', exit_code=0,
                stdout=json.dumps({'head':'a'*40, 'branch':'owned', 'dirty':''}), stderr='', duration_seconds=0)
            with patch.object(backend, 'run', return_value=result) as run:
                backend.inspect_git_metadata(assignment, workspace_path=root, source_path=root)
            self.assertTrue(Path('/usr/bin/env').is_file())
            self.assertIn('PATH=/usr/local/bin:/usr/bin:/bin', run.call_args.kwargs['argv'])
            self.assertIn('-I', run.call_args.kwargs['argv'])
            self.assertIn('-S', run.call_args.kwargs['argv'])
            restricted = run.call_args.args[0]
            self.assertEqual(restricted.id, assignment.id)
            self.assertEqual(restricted.execution_id, assignment.execution_id)
            self.assertEqual(restricted.fence, assignment.fence)
            self.assertEqual(restricted.execution_profile_definition, assignment.execution_profile_definition)
            self.assertEqual(assignment.model_dump(mode='json'), original_assignment)
            self.assertEqual(restricted.sandbox, 'read-only')
            self.assertFalse(restricted.network.enabled)
            self.assertEqual(run.call_args.kwargs['git_metadata_path'], root / '.git')
            self.assertNotIn('trusted_writable_mounts', run.call_args.kwargs)
            with patch.dict(os.environ, {'GIT_CONFIG_COUNT':'1', 'GIT_CONFIG_KEY_0':'core.fsmonitor',
                                         'GIT_CONFIG_VALUE_0':'untrusted', 'GIT_EXEC_PATH':'untrusted'}):
                env = backend.minimal_environment(extra=run.call_args.kwargs['environment'])
            self.assertNotIn('GIT_EXEC_PATH', env)
            self.assertNotIn('GIT_CONFIG_COUNT', env)
            self.assertEqual(env['GIT_CONFIG_GLOBAL'], '/dev/null')

    def test_untrusted_external_git_metadata_is_not_mounted(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); checkout=root/'checkout'; checkout.mkdir()
            source=root/'source'; source.mkdir(); (source/'.git').mkdir()
            other=root/'unrelated'; other.mkdir()
            (checkout/'.git').write_text('gitdir: '+str(other))
            backend=BubblewrapExecutionBackend()
            with patch.object(backend, 'run') as run, self.assertRaisesRegex(LocalExecutionPolicyError, 'canonical repository'):
                backend.inspect_git_metadata(worker_fixtures._assignment(), workspace_path=checkout, source_path=source)
            run.assert_not_called()

    def test_runtime_rechecks_canonical_assignment_ownership_fence_profile_and_lease(self):
        from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime, LocalExecutionWorkerRuntimeError
        from tests import test_control_plane_broker as broker_fixtures
        assignment=broker_fixtures.ControlPlaneBrokerTests._assignment()
        runtime=object.__new__(LocalExecutionWorkerRuntime)
        runtime.worker=SimpleNamespace(id=assignment.assigned_worker_id)
        observed=[assignment]
        runtime._pending_assignment=lambda _id: observed[0]
        runtime.backend=SimpleNamespace(inspect_git_metadata=lambda *args, **kwargs: {'head':'fixture'})
        with patch.object(runtime.backend, 'inspect_git_metadata', return_value={'head':'fixture'}) as inspect:
            self.assertEqual(runtime.inspect_git_metadata(assignment, workspace_path=Path('/fixture'), source_path=Path('/source')), {'head':'fixture'})
            inspect.assert_called_once()
        for change in ({'assigned_worker_id':'other'}, {'fence':2}, {'created_by':'other'},
                       {'project_id':'other'}, {'execution_profile_id':'other'}, {'lease':None},
                       {'lease':assignment.lease.model_copy(update={'expires_at':time.time()-1})}):
            observed[0]=assignment.model_copy(update=change)
            with self.subTest(change=change), patch.object(runtime.backend, 'inspect_git_metadata') as inspect:
                with self.assertRaises(LocalExecutionWorkerRuntimeError):
                    runtime.inspect_git_metadata(assignment, workspace_path=Path('/fixture'), source_path=Path('/source'))
                inspect.assert_not_called()


class IsolatedGitMetadataMarkerTests(unittest.TestCase):
    def setUp(self):
        self.backend=BubblewrapExecutionBackend()
        status=self.backend.probe()
        if not status.ready:
            if os.environ.get('CODEX_WEB_REQUIRE_ISOLATED_GIT_QUALIFICATION') == '1':
                self.fail('Required existing Bubblewrap isolation unavailable: '+str(status.reason))
            self.skipTest('Existing Bubblewrap isolation unavailable: '+str(status.reason))
        self.temporary=tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name); self.checkout=self.root/'repo'; self.checkout.mkdir()
        self.assignment=worker_fixtures._assignment()
        self.git('init','-b','owned')
        self.git('config','user.name','Fixture'); self.git('config','user.email','fixture@example.invalid')
        (self.checkout/'.gitattributes').write_text('source.txt filter=marker\n')
        (self.checkout/'source.txt').write_text('original\n')
        self.git('add','.'); self.git('commit','-m','fixture')
        self.head=self.git('rev-parse','HEAD')

    def git(self,*args):
        return subprocess.run(['/usr/bin/git','-C',str(self.checkout),*args],check=True,
                              capture_output=True,text=True).stdout.strip()

    def inspect(self):
        return self.backend.inspect_git_metadata(self.assignment, workspace_path=self.checkout, source_path=self.checkout)

    def test_fsmonitor_and_clean_filter_execute_only_inside_readonly_isolation(self):
        markers=[]
        for helper in ('fsmonitor','filter'):
            marker=self.root/(helper+'-host-marker'); markers.append(marker)
            script=self.checkout/(helper+'.sh')
            script.write_text('#!/bin/sh\ntouch "'+str(marker)+'"\n'+('cat\n' if helper=='filter' else '')+'exit 0\n')
            script.chmod(0o755)
        # A worker checkout must not replace the trusted reader's stdlib.
        for module in ('json.py', 'subprocess.py', 'sitecustomize.py'):
            (self.checkout/module).write_text('raise RuntimeError("untrusted reader override")\n')
        self.git('add','fsmonitor.sh','filter.sh','json.py','subprocess.py','sitecustomize.py'); self.git('commit','-m','controlled helpers')
        self.head=self.git('rev-parse','HEAD')
        self.git('config','core.fsmonitor',str(self.checkout/'fsmonitor.sh'))
        self.git('config','filter.marker.clean',str(self.checkout/'filter.sh'))
        self.git('config','filter.marker.smudge',str(self.checkout/'filter.sh'))
        # Force content refresh while preserving bytes. The harmless host
        # reproduction proves these exact helpers are executable configuration.
        (self.checkout/'source.txt').write_text('original\n')
        self.git('status','--porcelain')
        self.assertTrue(all(marker.exists() for marker in markers))
        for marker in markers: marker.unlink()
        (self.checkout/'source.txt').write_text('original\n')
        with patch.dict(os.environ, {'GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'core.fsmonitor',
                                     'GIT_CONFIG_VALUE_0':str(self.checkout/'fsmonitor.sh')}):
            observed=self.inspect()
        self.assertEqual(observed, {'head':self.head,'branch':'owned','dirty':''})
        self.assertTrue(all(not marker.exists() for marker in markers))

    def test_clean_dirty_and_changed_head_are_exact_observations(self):
        self.assertEqual(self.inspect(), {'head':self.head,'branch':'owned','dirty':''})
        (self.checkout/'source.txt').write_text('changed\n')
        self.assertIn('source.txt',self.inspect()['dirty'])
        self.git('add','source.txt'); self.git('commit','-m','new head')
        observed=self.inspect()
        self.assertNotEqual(observed['head'],self.head)
        self.assertEqual(observed['head'],self.git('rev-parse','HEAD'))
        self.assertEqual(observed['dirty'],'')
