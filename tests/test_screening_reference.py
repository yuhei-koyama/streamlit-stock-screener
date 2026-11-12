import hashlib
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from scripts import package_screening_reference as publisher


class ScreeningReferenceTests(unittest.TestCase):
    def fixture(self,root):
        source=root/'source'
        source.mkdir()
        (source/'run.json').write_text(json.dumps(dict(status='completed',completed_cases=560)),encoding='utf-8')
        rows=itertools.product(['2016_2020','2021_2025'],['stock','etf'],publisher.STRATEGIES,'ABCD',publisher.STOPS)
        pd.DataFrame(rows,columns=['period','asset_class','strategy_id','exit_policy','initial_stop']).to_csv(source/'summary.csv',index=False)
        return source

    def test_failed_publish_preserves_pointer_and_can_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            source=self.fixture(root)
            output=root/'bundles'
            output.mkdir()
            original=json.dumps({'bundle':'previous'})
            (output/'active.json').write_text(original,encoding='utf-8')
            with patch.object(publisher,'_write_bundle',side_effect=RuntimeError('中断')):
                with self.assertRaises(RuntimeError):
                    publisher.package(source,output)
            self.assertEqual((output/'active.json').read_text(encoding='utf-8'),original)

            def write_bundle(source,stage,summary,metadata):
                (stage/'summary.csv').write_text('検算用\n',encoding='utf-8')
                proof={'output_hashes':{'summary.csv':hashlib.sha256((stage/'summary.csv').read_bytes()).hexdigest()}}
                (stage/'source.json').write_text(json.dumps(proof),encoding='utf-8')

            with patch.object(publisher,'_write_bundle',side_effect=write_bundle) as writer:
                publisher.package(source,output)
                self.assertEqual(writer.call_count,1)
                pointer=json.loads((output/'active.json').read_text(encoding='utf-8'))
                self.assertNotEqual(pointer['bundle'],'previous')
                publisher.package(source,output)
                self.assertEqual(writer.call_count,1)
                (output/pointer['bundle']/'summary.csv').write_text('変更',encoding='utf-8')
                with self.assertRaises(ValueError):
                    publisher.package(source,output)

    def test_missing_case_is_rejected_before_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            source=self.fixture(root)
            frame=pd.read_csv(source/'summary.csv')
            frame.iloc[:-1].to_csv(source/'summary.csv',index=False)
            with self.assertRaises(ValueError):
                publisher.package(source,root/'bundles')
            self.assertFalse((root/'bundles').exists())


if __name__=='__main__':
    unittest.main()
