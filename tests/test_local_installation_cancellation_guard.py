import fcntl
from pathlib import Path
import subprocess
import tempfile
import unittest

from codex_web.local_deployment import unchanged_local_installation


class LocalInstallationCancellationGuardTests(unittest.TestCase):
    def test_real_checkout_readback_and_shared_installer_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git','init',str(root)],check=True,capture_output=True)
            subprocess.run(['git','-C',str(root),'-c','user.name=Fixture',
                            '-c','user.email=fixture@example.invalid','commit','--allow-empty',
                            '-m','fixture'],check=True,capture_output=True)
            revision = subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()
            lock_path = root/'installer.lock'
            with unchanged_local_installation(root, revision, lock_path=lock_path) as observed:
                self.assertEqual(observed, revision)
                with lock_path.open('a') as installer:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(installer, fcntl.LOCK_EX|fcntl.LOCK_NB)
            with lock_path.open('a') as installer:
                fcntl.flock(installer, fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaisesRegex(ValueError,'installer is active'):
                    with unchanged_local_installation(root, revision, lock_path=lock_path):
                        self.fail('active installer escaped guard')
            for build in ('unknown','a'*40):
                with self.assertRaises(ValueError):
                    with unchanged_local_installation(root, build, lock_path=lock_path):
                        self.fail('unknown or mismatched build escaped guard')
