from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from scripts.run_finite_capital_backtest import run
from stock_screener.market_research import freeze, frozen_assets
from stock_screener.trading_calendar import calendar_metadata


def prepared_fixture(root):
    listing = root / 'listing.csv'
    etfs = root / 'etfs.csv'
    pd.DataFrame({'日付': [20261031, 20261031], 'コード': ['1111', '1306'],
                  '銘柄名': ['普通株', 'ETF'],
                  '市場・商品区分': ['プライム（内国株式）', 'ETF・ETN']}).to_csv(listing, index=False)
    pd.DataFrame({'コード': ['1306']}).to_csv(etfs, index=False)
    universe = root / 'universe'
    freeze(listing, etfs, universe)
    meta, assets = frozen_assets(universe)
    data = root / 'data'
    data.mkdir()
    days = pd.date_range('2021-01-04', periods=3)
    prices = pd.DataFrame(dict(Open=100., High=101., Low=99., Close=100., Volume=1000.), index=days)
    execution = prices.assign(LotSize=1, Split=1., Dividend=0., DividendPayDate=pd.NaT)
    for ticker in assets.ticker:
        prices.to_csv(data / f'signals_{ticker}.csv', index_label='Date')
        execution.to_csv(data / f'execution_{ticker}.csv', index_label='Date')
    pd.DataFrame({'Date': days}).to_csv(data / 'calendar.csv', index=False)
    manifest = {'universe_hash': meta['universe_hash'], 'trading_calendar': calendar_metadata(),
                'prepared_assets': assets.to_dict('records'), 'prepared_count': 2,
                'status': 'prepared_partial_universe',
                'execution_price_basis': 'adjusted_ohlc_virtual_shares',
                'dividend_mode': 'embedded_in_adjusted_prices',
                'file_hashes': {p.name: sha256(p.read_bytes()).hexdigest() for p in data.iterdir()}}
    (data / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return data, universe


class FiniteCapitalCliTests(unittest.TestCase):
    def test_partial_universe_cannot_run_as_primary_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data, universe = prepared_fixture(root)
            with self.assertRaisesRegex(ValueError, '部分入力'):
                run(data, universe, root / 'output')
            self.assertFalse((root / 'output').exists())

    def test_changed_prepared_file_stops_before_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data, universe = prepared_fixture(root)
            path = data / 'signals_1111.T.csv'
            path.write_bytes(path.read_bytes() + b'\n')
            with self.assertRaisesRegex(ValueError, '変わっています'):
                run(data, universe, root / 'output', audit=True)
            self.assertFalse((root / 'output').exists())

    def test_missing_warmup_reference_stops_before_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data, universe = prepared_fixture(root)
            with self.assertRaisesRegex(ValueError, '期待取引日の取得欠損'):
                run(data, universe, root / 'output', audit=True)
            self.assertFalse((root / 'output').exists())
            failure = json.loads((root / 'output.preflight-failure.json').read_text(encoding='utf-8'))
            self.assertTrue(failure['missing_dates']['rs_reference'])

    def test_missing_price_basis_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data, universe = prepared_fixture(root)
            path = data / 'manifest.json'
            manifest = json.loads(path.read_text(encoding='utf-8'))
            del manifest['execution_price_basis']
            path.write_text(json.dumps(manifest), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '調整OHLC'):
                run(data, universe, root / 'output', audit=True)
            self.assertFalse((root / 'output').exists())

    def test_primary_execution_requires_official_benchmark_input(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data, universe = prepared_fixture(root)
            path = data / 'manifest.json'
            manifest = json.loads(path.read_text(encoding='utf-8'))
            manifest['status'] = 'prepared_complete_universe'
            path.write_text(json.dumps(manifest), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'benchmark-nav'):
                run(data, universe, root / 'output')
            self.assertFalse((root / 'output').exists())
