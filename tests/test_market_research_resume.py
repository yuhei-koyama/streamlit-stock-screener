"""F01。フェーズごとの依存入力と成果物の世代を保護する。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from stock_screener import market_research
from stock_screener.exploratory_data import digest
from stock_screener.linda_versions import TV
from tests.test_market_research import master


class MarketResearchResumeTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        listing, etfs, _, _ = master(self.root)
        self.work = self.root/'research'
        universe = market_research.freeze(listing, etfs, self.work)
        cache = self.work/'cache'
        cache.mkdir()
        dates = pd.bdate_range('2015-01-05', periods=20)
        for start in ['2016-01-04', '2019-01-04', '2021-01-04']:
            dates = dates.union(pd.bdate_range(start, periods=20))
        dates = dates.union(pd.DatetimeIndex(['2020-12-30', '2025-12-30'])).sort_values()
        prices = pd.DataFrame(dict(Open=100., High=101., Low=99., Close=100., Volume=1000.), index=dates)
        assets = {}
        for ticker in ['1306.T', '130A.T']:
            path = cache/f'{ticker}.csv'
            prices.to_csv(path, index_label='Date')
            assets[ticker] = dict(status='ready', hashes={path.name:digest(path)})
        self.collection_path = self.work/'collection.json'
        self.collection_path.write_text(json.dumps(dict(
            kind='synthetic', universe_hash=universe['universe_hash'], assets=assets)), encoding='utf-8')
        self.enterContext(patch.multiple(market_research, STRATEGIES=[TV], HORIZONS=(5,), STOPS=(.08,)))
        market_research.run_batch(self.work, limit=2, phase='entry')

    def replace_prices(self, ticker):
        path = self.work/'cache'/f'{ticker}.csv'
        prices = pd.read_csv(path, index_col='Date', parse_dates=['Date'])
        prices['Close'] = 100.5
        prices.to_csv(path, index_label='Date')
        collection = json.loads(self.collection_path.read_text(encoding='utf-8'))
        collection['assets'][ticker]['hashes'][path.name] = digest(path)
        self.collection_path.write_text(json.dumps(collection), encoding='utf-8')

    def test_unchanged_inputs_resume_and_keep_entry_record(self):
        folder = self.work/'results/130A.T'
        before = json.loads((folder/'status.json').read_text(encoding='utf-8'))['phase_records']['entry']
        self.assertEqual(set(before['dependencies']['input_hashes']), {'1306.T.csv', '130A.T.csv'})
        self.assertEqual(market_research.run_batch(self.work, limit=2, phase='exits'), 2)
        after = json.loads((folder/'status.json').read_text(encoding='utf-8'))
        self.assertEqual(after['phase_records']['entry'], before)
        self.assertEqual(set(after['phase_records']), {'entry', 'exits'})
        self.assertEqual(market_research.run_batch(self.work, limit=2, phase='entry'), 0)
        self.assertEqual(market_research.aggregate(self.work, phase='entry')['status'], 'completed')
        self.assertEqual(market_research.aggregate(self.work, phase='exits')['status'], 'completed')

    def test_updated_asset_cannot_retag_old_entry_or_write_other_phase(self):
        self.replace_prices('130A.T')
        files = {path:path.read_bytes() for path in (self.work/'results').rglob('*') if path.is_file()}
        for phase in ['entry', 'exits']:
            with self.assertRaisesRegex(ValueError, '前回と異なります'):
                market_research.run_batch(self.work, limit=2, phase=phase)
        with self.assertRaisesRegex(ValueError, '前回と異なります'):
            market_research.aggregate(self.work, phase='entry')
        for path, content in files.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertFalse(list((self.work/'results').rglob('exits.zip')))

    def test_updated_reference_invalidates_stock_even_without_reference_result(self):
        status = self.work/'results/1306.T/status.json'
        status.rename(status.with_name('status.saved.json'))
        self.replace_prices('1306.T')
        with self.assertRaisesRegex(ValueError, 'RS参照'):
            market_research.run_batch(self.work, limit=2, phase='exits')
        with self.assertRaisesRegex(ValueError, 'RS参照'):
            market_research.aggregate(self.work, phase='entry')

    def test_changed_calendar_rejects_resume_and_aggregation(self):
        changed = dict(market_research.calendar_metadata(), policy='changed_calendar')
        with patch.object(market_research, 'calendar_metadata', return_value=changed):
            with self.assertRaisesRegex(ValueError, 'カレンダー'):
                market_research.run_batch(self.work, limit=2, phase='exits')
            with self.assertRaisesRegex(ValueError, 'カレンダー'):
                market_research.aggregate(self.work, phase='entry')

    def test_changed_code_rejects_resume_and_aggregation(self):
        def changed_digest(path):
            return 'changed_code' if Path(path).suffix == '.py' else digest(path)
        with patch.object(market_research, 'digest', side_effect=changed_digest):
            with self.assertRaisesRegex(ValueError, 'コード'):
                market_research.run_batch(self.work, limit=2, phase='exits')
            with self.assertRaisesRegex(ValueError, 'コード'):
                market_research.aggregate(self.work, phase='entry')

    def test_legacy_result_requires_new_workspace(self):
        path = self.work/'results/130A.T/status.json'
        status = json.loads(path.read_text(encoding='utf-8'))
        status.pop('phase_records')
        path.write_text(json.dumps(status), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '旧結果'):
            market_research.run_batch(self.work, limit=2, phase='exits')
        with self.assertRaisesRegex(ValueError, '旧結果'):
            market_research.aggregate(self.work, phase='entry')

    def test_modified_old_archive_is_rejected_before_new_phase(self):
        archive = self.work/'results/130A.T/entry.zip'
        archive.write_bytes(archive.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError, '保存結果のハッシュ'):
            market_research.run_batch(self.work, limit=2, phase='exits')
        with self.assertRaisesRegex(ValueError, '保存結果のハッシュ'):
            market_research.aggregate(self.work, phase='entry')
        self.assertFalse(list((self.work/'results').rglob('exits.zip')))

    def test_phase_records_are_authoritative_for_aggregation(self):
        for ticker in ['1306.T', '130A.T']:
            path = self.work/'results'/ticker/'status.json'
            status = json.loads(path.read_text(encoding='utf-8'))
            status.update(kind='market', input_hash='stale_summary_hash', code_hashes={'summary':'stale'})
            path.write_text(json.dumps(status), encoding='utf-8')
        self.assertEqual(market_research.aggregate(self.work, phase='entry')['kind'], 'synthetic')
        self.assertEqual(market_research.run_batch(self.work, limit=2, phase='entry'), 0)
