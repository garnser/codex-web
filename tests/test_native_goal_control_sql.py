"""Execute actual dependency SQL, using two disposable SQLite connections only."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

BASELINE = '--baseline' in sys.argv
if BASELINE:
    sys.argv.remove('--baseline')
SQL = Path(__file__).resolve().parents[1] / 'patches/codex/goal-control-v1/sql'

class NativeGoalSQLTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / 'synthetic.sqlite'
        self.db = sqlite3.connect(path)
        self.writer = sqlite3.connect(path)
        self.addCleanup(self.db.close)
        self.addCleanup(self.writer.close)
        self.db.executescript((SQL/'0001_thread_goals.sql').read_text())
        if not BASELINE:
            self.db.executescript((SQL/'0003_goal_control.sql').read_text())
        self.db.execute("INSERT INTO thread_goals VALUES('t','g','original','blocked',200,75,9,1,1)")
        self.db.commit()

    def hold(self):
        self.db.execute("UPDATE goal_control_clock SET held=1,epoch=1 WHERE thread_id='t'")
        self.db.commit()

    def test_hold_fences_separate_connection_status_and_accounting_writers(self):
        self.hold()
        for assignment in ("status='paused'", 'tokens_used=99', "thread_id='other'"):
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'goal writer fenced'):
                self.writer.execute('UPDATE thread_goals SET '+assignment)
            self.writer.rollback()

    def test_same_goal_write_advances_revision_and_stales_control(self):
        self.writer.execute('UPDATE thread_goals SET tokens_used=tokens_used+1')
        self.writer.commit()
        self.assertEqual(self.db.execute('SELECT revision FROM goal_control_clock').fetchone()[0], 2)
        self.assertEqual(self.db.execute('UPDATE goal_control_clock SET held=1 WHERE revision=1').rowcount, 0)

    def test_status_only_query_preserves_objective_and_all_accounting(self):
        snapshot = self.db.execute((SQL/'status.sql').read_text(), ('paused',2,'t','g')).fetchone()
        value = json.loads(snapshot[0])
        self.assertEqual([value[k] for k in ('objective','tokenBudget','tokensUsed','timeUsedSeconds','createdAtMs')], ['original',200,75,9,1])
        self.assertEqual(self.db.execute('SELECT revision FROM goal_control_clock').fetchone()[0],2)

    def test_foreign_and_budget_limited_requests_do_not_mutate(self):
        query=(SQL/'status.sql').read_text()
        self.assertIsNone(self.db.execute(query,('paused',2,'t','foreign')).fetchone())
        self.db.execute('UPDATE thread_goals SET tokens_used=200')
        self.assertIsNone(self.db.execute(query,('active',2,'t','g')).fetchone())
        self.assertEqual(self.db.execute('SELECT status FROM thread_goals').fetchone()[0], 'blocked')

    def test_receipt_cannot_be_changed_or_deleted(self):
        self.db.execute("INSERT INTO goal_control_receipts VALUES('t','op','{}','{}')")
        self.db.commit()
        for query in ('UPDATE goal_control_receipts SET snapshot=\'changed\'', 'DELETE FROM goal_control_receipts'):
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable goal receipt'):
                self.writer.execute(query)
            self.writer.rollback()

if __name__ == '__main__':
    unittest.main()
