from contextlib import redirect_stdout
from hashlib import sha256
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from scripts import audit_finite_capital_market as auditor
from stock_screener.market_research import freeze


def fixture(root):
    listing = root / 'listing.csv'
    etfs = root / 'etfs.csv'
    pd.DataFrame({'日付': [20261031, 20261031], 'コード': ['1111', '1306'],
                  '銘柄名': ['普通株', 'ETF'],
                  '市場・商品区分': ['プライム（内国株式）', 'ETF・ETN']}).to_csv(listing, index=False)
    pd.DataFrame({'コード': ['1306']}).to_csv(etfs, index=False)
    work = root / 'universe'
    meta = freeze(listing, etfs, work)
    cache = work / 'cache'
    cache.mkdir()
    dates = pd.date_range('2016-01-04', periods=3)
    prices = pd.DataFrame(dict(Open=100., High=101., Low=99., Close=100., Volume=1000.), index=dates)
    prices.loc[dates[0], 'High'] = 99.95
    prices.loc[dates[1], 'High'] = 100. - 1e-13
    prices.loc[dates[2], 'Volume'] = 0.
    prices.to_csv(cache / '1111.T.csv', index_label='Date', float_format='%.17g')
    (cache / '1111.T.json').write_text('{}')
    (cache / '1306.T.json').write_text('{"error":"Bad Request"}')
    states = {'1111.T': {'status': 'ready', 'hashes': {p.name: sha256(p.read_bytes()).hexdigest()
                         for p in [cache / '1111.T.csv', cache / '1111.T.json']}},
              '1306.T': {'status': 'blocked_data', 'reason': 'HTTP400',
                         'hashes': {'1306.T.json': sha256((cache / '1306.T.json').read_bytes()).hexdigest()}}}
    (work / 'collection.json').write_text(json.dumps({'universe_hash': meta['universe_hash'], 'assets': states}))
    return work


class MarketAuditTests(unittest.TestCase):
    def test_records_proposals_zero_volume_and_data_block_without_correction(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work = fixture(root)
            original = (work / 'cache/1111.T.csv').read_bytes()
            with patch.object(auditor, 'approved_rows', return_value={}), redirect_stdout(io.StringIO()):
                result = auditor.audit(work, root / 'audit')
            self.assertEqual(result['unapproved_correction_rows'], 2)
            self.assertEqual(result['unapproved_substantive_rows'], 1)
            self.assertEqual(result['unapproved_rounding_rows'], 1)
            self.assertEqual(result['invalid_price_rows'], 0)
            self.assertTrue(result['collection_complete'])
            self.assertFalse(result['corrections_applied'])
            self.assertEqual(result['status_counts']['blocked_data'], 1)
            self.assertEqual((work / 'cache/1111.T.csv').read_bytes(), original)
            coverage = pd.read_csv(root / 'audit/coverage.csv')
            self.assertEqual(coverage.loc[coverage.ticker.eq('1111.T'), 'zero_volume_rows'].iloc[0], 1)

    def test_changed_input_rejected_before_creating_audit_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work = fixture(root)
            path = work / 'cache/1111.T.csv'
            path.write_bytes(path.read_bytes() + b'\n')
            with patch.object(auditor, 'approved_rows', return_value={}):
                with self.assertRaisesRegex(ValueError, 'ハッシュが変わっています'):
                    auditor.audit(work, root / 'audit')
            self.assertFalse((root / 'audit').exists())

    def test_previous_approval_requires_matching_row_and_source_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work = fixture(root)
            with patch.object(auditor, 'approved_rows', return_value={}), redirect_stdout(io.StringIO()):
                auditor.audit(work, root / 'audit1')
            row = pd.read_csv(root / 'audit1/proposed_corrections.csv', float_precision='round_trip').iloc[0].to_dict()
            key = (row['ticker'], row['Date'])
            with patch.object(auditor, 'approved_rows', return_value={key: row}), redirect_stdout(io.StringIO()):
                matched = auditor.audit(work, root / 'audit2')
            self.assertEqual(matched['previously_approved_matching_rows'], 1)
            row['source_sha256'] = 'wrong'
            with patch.object(auditor, 'approved_rows', return_value={key: row}), redirect_stdout(io.StringIO()):
                unmatched = auditor.audit(work, root / 'audit3')
            self.assertEqual(unmatched['previously_approved_matching_rows'], 0)
            self.assertEqual(unmatched['unapproved_correction_rows'], 2)
