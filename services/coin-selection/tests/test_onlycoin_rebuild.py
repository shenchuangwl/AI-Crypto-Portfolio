import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from test_onlycoin_ledger import ROOT, batch

SCRIPT = ROOT / 'scripts/rebuild_onlycoin_history.py'


class OnlyCoinRebuildTests(unittest.TestCase):
    def test_plan_then_explicit_apply_new_database_and_reject_overwrite(self):
        self.assertTrue(SCRIPT.exists(), 'safe rebuild CLI missing')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox = root / 'inbox'
            inbox.mkdir()
            for seq in (0, 1):
                b = batch(seq, (f'T{seq}USDT',))
                (inbox / (b['scan_id'] + '.candidates.json')).write_text(json.dumps(b))
            db = root / 'onlycoin.sqlite'
            args = [sys.executable, '-B', str(SCRIPT), '--inbox-dir', str(inbox),
                    '--from', '2026-09-09', '--to', '2026-09-09', '--db', str(db)]
            planned = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(planned.returncode, 0, planned.stderr)
            self.assertEqual(json.loads(planned.stdout)['mode'], 'plan')
            self.assertFalse(db.exists())
            applied = subprocess.run(args + ['--apply'], capture_output=True, text=True)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            report = json.loads(applied.stdout)
            self.assertEqual(report['committed_batches'], 2)
            self.assertEqual(report['days'][0]['counts']['onlycoin'], 2)
            self.assertEqual(report['days'][0]['coverage']['availability_quality'], 'PROXY')
            self.assertTrue(db.exists())
            before = db.read_bytes()
            rejected = subprocess.run(args + ['--apply'], capture_output=True, text=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertEqual(before, db.read_bytes())
            old = root / 'ledger.sqlite'
            args[-1] = str(old)
            rejected = subprocess.run(args + ['--apply'], capture_output=True, text=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse(old.exists())


if __name__ == '__main__':
    unittest.main()
