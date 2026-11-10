"""Real builder and credential CAS qualify the routing fingerprint boundary."""
import time
import unittest

from codex_web.owner_routing import OwnerRoutingRecipient
from codex_web.services.owner_routing import OwnerRoutingService
from tests import test_native_procedure_credential_custody as fixtures


class AcceptedInitialSealCanonicalCustodyTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_initial_credential_admission_reconstructs_original_authority(self):
        fixture = fixtures.NativeProcedureCredentialCustodyTests()
        await fixture.asyncSetUp()
        self.addAsyncCleanup(fixture.asyncTearDown)
        fixture.bootstrap()
        pending = fixture.current()
        self.assertIsNotNone(pending.native_credential_custody)
        self.assertIsNone(pending.native_credential_custody.admitted_at)
        self.assertEqual(pending.secret_refs, ())
        # Only exercise the routing checksum boundary here. Retained START and
        # ACK are covered by AcceptedPreparedOwnerRoutingTests, not fabricated
        # by this canonical builder/SecretBroker/assignment-writer fixture.
        routing = OwnerRoutingService.__new__(OwnerRoutingService)
        routing.workers = fixture.f.workers.store.worker
        witness = OwnerRoutingRecipient(thread_id='custody-native-thread', bootstrap_id='private-original-binding',
            assignment_id=pending.id, execution_id=pending.execution_id,
            worker_id=pending.assigned_worker_id, fence=pending.fence,
            authority_checksum=routing._authority_checksum(pending), work_item_owner_checksum='a' * 64)
        discovery, _ = await fixture.admit()
        self.assertTrue(discovery.admitted)
        sealed = fixture.current()
        self.assertIsNotNone(sealed.native_credential_custody.admitted_at)
        self.assertEqual(sealed.secret_refs, (fixture.provider_secret.id,))
        self.assertNotEqual(routing._authority_checksum(sealed), witness.authority_checksum)
        self.assertTrue(routing._initial_seal_authority_compatible(sealed, witness, started_at=time.time()))
        reconstructed = sealed.model_copy(update={'secret_refs': pending.secret_refs})
        self.assertEqual(routing._authority_checksum(reconstructed), witness.authority_checksum)
        # Actual canonical CAS never invokes the external action/model provider.
        self.assertEqual(fixture.f.ops.calls, [])
