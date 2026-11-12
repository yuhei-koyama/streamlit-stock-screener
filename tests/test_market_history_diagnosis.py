from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from scripts.diagnose_market_history import diagnose
from tests.test_finite_capital_market_audit import fixture


class MarketHistoryDiagnosisTests(unittest.TestCase):
    def test_saves_response_and_resumes_without_duplicate_requests(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work = fixture(root)
            payload = {'chart': {'result': [{'meta': {'symbol': '1306.T', 'dataGranularity': '1d'},
                       'timestamp': [int(pd.Timestamp('2026-01-05', tz='UTC').timestamp())],
                       'indicators': {'quote': [{'close': [100.]}]}}], 'error': None}}
            class Response:
                status_code = 200
                content = json.dumps(payload).encode()
                def json(self):
                    return payload
            with patch('scripts.diagnose_market_history.requests.Session') as factory, redirect_stdout(io.StringIO()):
                get = factory.return_value.__enter__.return_value.get
                get.return_value = Response()
                result = diagnose(work, root / 'diagnosis', pause=0)
                diagnose(work, root / 'diagnosis', pause=0)
                self.assertEqual(get.call_count, 1)
            self.assertEqual(result['checked_tickers'], 1)
            self.assertEqual(result['historical_data_returned_tickers'], 0)
            self.assertEqual(result['assets']['1306.T']['first_observed_date'], '2026-01-05')
            collection = work / 'collection.json'
            collection.write_bytes(collection.read_bytes() + b' ')
            with self.assertRaisesRegex(ValueError, '入力またはコードが変わっています'):
                diagnose(work, root / 'diagnosis', pause=0)

    def test_rate_limit_stops_and_keeps_raw_response(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work = fixture(root)
            class Response:
                status_code = 429
                content = b'rate limited'
                def raise_for_status(self):
                    raise RuntimeError('rate limited')
            with patch('scripts.diagnose_market_history.requests.Session') as factory:
                get = factory.return_value.__enter__.return_value.get
                get.return_value = Response()
                with self.assertRaisesRegex(RuntimeError, 'rate limited'):
                    diagnose(work, root / 'diagnosis', pause=0)
                self.assertEqual(get.call_count, 1)
            state = json.loads((root / 'diagnosis/run.json').read_text(encoding='utf-8'))
            self.assertEqual(state['status'], 'interrupted')
            self.assertEqual((root / 'diagnosis/1306.T.json').read_bytes(), b'rate limited')
