"""Isolated raw-input contract tests, never load current caches."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from coin_selection import board_projection as bp

class InputContract(unittest.TestCase):
    def test_missing_pit_blocks(self):
        self.assertTrue(hasattr(bp, 'prepare_v13_rows'), 'missing fail-closed raw-input adapter')
        with self.assertRaisesRegex(ValueError, 'INPUT_INCOMPLETE'):
            bp.prepare_v13_rows([{'symbol':'AAAUSDT'}], scan_id='20260906-048', now_ms=1788696000000)

if __name__ == '__main__': unittest.main(verbosity=2)
