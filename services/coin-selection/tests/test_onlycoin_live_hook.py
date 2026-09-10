"""Live hook must not alter existing boards or execution permissions."""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT/'services/coin-selection/src'))
from coin_selection import board_projection as bp


class Hook(unittest.TestCase):
    def test_y_uses_persisted_final_batch_and_live_availability(self):
        self.assertTrue(callable(getattr(bp,'record_onlycoin_batch',None)), 'live OnlyCoin hook missing')
        seen=[]
        class Store:
            def __init__(self,path): seen.append(path)
            def ingest_batch(self,batch,**kw): seen.append((batch,kw));return {'status':'ok'}
            def close(self): seen.append('closed')
        fake=types.ModuleType('coin_selection.onlycoin_ledger')
        setattr(fake,'OnlyCoinLedger',Store)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules,{'coin_selection.onlycoin_ledger':fake}):
            root=Path(tmp); inbox=root/'inbox';inbox.mkdir()
            batch={'board_key':'y','scan_id':'20260909-044','candidates':[], 'dmr_executable':False}
            (inbox/'20260909-044.candidates.json').write_text(json.dumps(batch))
            out=bp.record_onlycoin_batch('y',root,inbox,'20260909-044')
            self.assertEqual(out['status'],'ok')
            self.assertEqual(seen[1][0],batch)
            self.assertEqual(seen[1][1]['provenance'],'live_committed')
            self.assertTrue(seen[1][1]['available_at'].endswith('Z'))
            self.assertEqual(seen[-1],'closed')

    def test_main_and_x_are_noop(self):
        self.assertTrue(callable(getattr(bp,'record_onlycoin_batch',None)))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for key in ('main','x'):
                self.assertEqual(bp.record_onlycoin_batch(key,root,root,'20260909-044')['status'],'not_applicable')
            self.assertEqual(list(root.iterdir()),[])

    def test_hook_failure_is_visible_without_raising_into_main(self):
        self.assertTrue(callable(getattr(bp,'record_onlycoin_batch',None)))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.assertEqual(bp.record_onlycoin_batch('y',root,root,'20260909-044')['status'],'error')

if __name__=='__main__':unittest.main()
