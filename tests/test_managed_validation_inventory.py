"""Complete bounded managed custody inventory; isolated canonical Artifact/Evidence."""
import json
import os
import unittest
from codex_web.artifact_evidence import EvidenceResult
from codex_web.storage.state_store import StorageOperationBudget,storage_operation_budget
from tests.test_managed_validation_settlement import ManagedCaptureSettlementTests


class ManagedValidationInventoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture=ManagedCaptureSettlementTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.capture=self.fixture.capture
        for path in self.capture.directory.glob('report-*.json'):path.unlink()

    def reports(self,count):
        for index in range(1,count+1):
            report={**self.fixture.report,'run_id':f'{index:032x}','exit_code':1 if index%4==0 else 0}
            (self.capture.directory/f'report-{index:032x}.json').write_text(json.dumps(report))

    def last(self,count,**changes):
        path=self.capture.directory/f'report-{count:032x}.json'
        report=json.loads(path.read_text());report.update(changes);path.write_text(json.dumps(report))
        return path

    def test_seventeen_finished_reports_capture_once_and_allow_renewal_with_known_nonzero_exits(self):
        self.reports(17)
        before={p.name:p.read_bytes() for p in self.capture.directory.glob('report-*.json')}
        ids=self.capture.capture()
        self.assertEqual(len(ids),17);self.assertEqual(self.capture.state,'captured')
        self.assertFalse(self.capture.blocks_assignment_renewal())
        self.assertEqual(self.capture.capture(),ids)
        artifacts,evidence=self.fixture.captures_and_evidence()
        self.assertEqual(len(artifacts),17);self.assertEqual(len(evidence),17)
        self.assertTrue(all(e.result==EvidenceResult.INFO and e.metadata['result']=='unknown' for e in evidence))
        self.assertEqual(sum(e.metadata['observed_exit_code']==1 for e in evidence),4)
        self.assertEqual({p.name:p.read_bytes() for p in self.capture.directory.glob('report-*.json')},before)
        self.assertTrue(all(a.metadata['registration_artifact_id']==self.capture.registration.id for a in artifacts))

    def test_existing_sixty_four_total_entry_bound_accepts_all_sixty_three_reports(self):
        self.reports(63)
        self.assertEqual(len(list(self.capture.directory.iterdir())),64)
        ids=self.capture.capture();self.assertEqual(len(ids),63)
        self.assertFalse(self.capture.blocks_assignment_renewal())
        self.assertEqual(len(self.fixture.captures_and_evidence()[1]),63)

    def test_running_seventeenth_report_blocks_after_finished_prefix(self):
        self.reports(16);self.capture.capture()
        self.reports(17);self.last(17,state='running',exit_code=None,finished_at=None)
        self.assertTrue(self.capture.blocks_assignment_renewal())
        self.assertEqual(len(self.capture.capture()),17)
        self.assertTrue(self.capture.blocks_assignment_renewal())

    def test_malformed_unknown_or_unattributed_seventeenth_report_fails_closed(self):
        self.reports(17)
        path=self.capture.directory/f'report-{17:032x}.json';valid=path.read_text()
        for changes in ({'state':'unknown'},{'exit_code':None},{'finished_at':None},{'run_id':'f'*32}):
            with self.subTest(changes=changes):
                path.write_text(valid);self.last(17,**changes)
                self.assertTrue(self.capture.blocks_assignment_renewal())
                with self.assertRaises(ValueError):self.capture.capture()
                self.assertEqual(self.capture.state,'unavailable')
        path.write_text('{invalid-json')
        self.assertTrue(self.capture.blocks_assignment_renewal())
        with self.assertRaises(ValueError):self.capture.capture()

    def test_overflow_refuses_before_any_prefix_artifacts_or_evidence(self):
        self.reports(64)  # 64 reports + the registered .gitignore = 65 entries.
        self.assertTrue(self.capture.blocks_assignment_renewal())
        with self.assertRaisesRegex(ValueError,'entry bound'):self.capture.capture()
        self.assertEqual(self.capture.state,'unavailable')
        self.assertEqual(self.fixture.captures_and_evidence(),([],[]))

    def test_unexpected_pending_entry_refuses_complete_inventory(self):
        self.reports(17);(self.capture.directory/'report.pending').write_text('incomplete')
        self.assertTrue(self.capture.blocks_assignment_renewal())
        with self.assertRaisesRegex(ValueError,'unknown entry'):self.capture.capture()
        self.assertEqual(self.fixture.captures_and_evidence(),([],[]))

    def test_report_symlink_and_nonregular_file_cannot_prove_settlement(self):
        self.reports(17);path=self.capture.directory/f'report-{17:032x}.json';path.unlink()
        path.symlink_to(self.fixture.workspace/'source.py')
        self.assertTrue(self.capture.blocks_assignment_renewal())
        with self.assertRaises(OSError):self.capture.capture()
        path.unlink();os.mkfifo(path)
        self.assertTrue(self.capture.blocks_assignment_renewal())
        with self.assertRaisesRegex(ValueError,'regular'):self.capture.capture()

    def test_byte_bound_and_cancelled_storage_budget_remain_fail_closed(self):
        self.reports(17);path=self.capture.directory/f'report-{17:032x}.json';path.write_bytes(b' '*16385)
        self.assertTrue(self.capture.blocks_assignment_renewal())
        with self.assertRaisesRegex(ValueError,'exceeds contract'):self.capture.capture()
        self.reports(17)
        token=storage_operation_budget.set(StorageOperationBudget(-1))
        try:
            self.assertTrue(self.capture.blocks_assignment_renewal())
            with self.assertRaises(TimeoutError):self.capture.capture()
        finally:storage_operation_budget.reset(token)
        self.assertEqual(self.capture.state,'unavailable')

    def test_missing_registration_does_not_become_renewable_from_finished_files(self):
        self.reports(17);self.capture.registration=None
        self.assertEqual(self.capture.capture(),())
        self.assertTrue(self.capture.blocks_assignment_renewal())
        self.assertEqual(self.fixture.captures_and_evidence(),([],[]))


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(ManagedValidationInventoryTests(name) for name in ManagedValidationInventoryTests.__dict__ if name.startswith('test_'))
