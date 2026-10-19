"""Deployment data indirection with the actual preservation transaction boundary."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from codex_web.paths import artifact_content_directory
from tests import test_recovery_storage_operator as fixture


class ArtifactContentStartupPathTests(unittest.TestCase):
    def test_pins_data_directory_across_deployment_link_retargeting(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / 'first'; first.mkdir()
            second = root / 'second'; second.mkdir()
            data = root / 'data'; data.symlink_to(first, target_is_directory=True)
            pinned = artifact_content_directory(data)
            data.unlink(); data.symlink_to(second, target_is_directory=True)
            pinned.mkdir(); (pinned / 'retained').write_bytes(b'original content')
            self.assertEqual((first / 'artifact-content' / 'retained').read_bytes(), b'original content')
            self.assertFalse((second / 'artifact-content').exists())

    def test_does_not_follow_a_content_leaf_symlink(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / 'data'; data.mkdir()
            outside = root / 'outside'; outside.mkdir()
            leaf = data / 'artifact-content'; leaf.symlink_to(outside, target_is_directory=True)
            pinned = artifact_content_directory(data)
            self.assertTrue(pinned.is_symlink())
            self.assertEqual(pinned, leaf)
            self.assertNotEqual(pinned, outside)


class ArtifactContentDeploymentPreservationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.StorageOperatorTests.asyncSetUp
    asyncTearDown = fixture.StorageOperatorTests.asyncTearDown
    publish = fixture.StorageOperatorTests.publish
    insert_opaque = fixture.StorageOperatorTests.insert_opaque
    client = fixture.StorageOperatorTests.client
    body = fixture.StorageOperatorTests.body
    committed = fixture.StorageOperatorTests.committed
    capture = fixture.StorageOperatorTests.capture
    assert_original = fixture.StorageOperatorTests.assert_original

    def configure_deployment(self):
        durable = self.root / 'durable'; durable.mkdir()
        self.local_content.root.rename(durable / 'artifact-content')
        installed = self.root / 'installed'; installed.mkdir()
        data = installed / 'data'; data.symlink_to(durable, target_is_directory=True)
        self.local_content.root = artifact_content_directory(data)
        return data, durable

    async def test_linked_deployment_captures_and_verifies_same_opaque_artifact(self):
        self.configure_deployment()
        await fixture.StorageOperatorTests.test_capture_verify_same_backend_once_preserves_invalid_rows_without_health_pass(self)

    async def test_content_leaf_symlink_still_refuses_before_capture(self):
        _, durable = self.configure_deployment()
        leaf = durable / 'artifact-content'
        target = durable / 'moved-content'; leaf.rename(target)
        leaf.symlink_to(target, target_is_directory=True)
        async with self.client() as client:
            response = await client.post('/api/recovery/storage-preservation/operations', json=self.body())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['item']['status'], 'uncertain')
        self.assertIn('RecoveryConflictError', response.json()['item']['last_error'])
        self.assertFalse(self.committed())
        self.assertFalse([a for a in self.artifacts.store.load().artifacts
                          if a.source == 'recovery-storage-preservation.v1'])
        self.assert_original()
