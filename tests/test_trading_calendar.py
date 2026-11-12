"""取引所休場日を指標・約定入力と営業日カウントから除く。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from scripts.validate_additional_period import prepare
from stock_screener.backtest import Config
from stock_screener.backtest_io import load_dataset
from stock_screener.backtest_signals import screening_signals
from stock_screener.entry_exit_diagnosis import fixed_horizon
from stock_screener.exploratory_data import digest
from stock_screener.independent_backtest import run_independent_backtest
from stock_screener.market_research import freeze, run_batch
from stock_screener.trading_calendar import split_trading_rows, trading_day_mask
from stock_screener.universe import STOCK_TICKERS
from tests.test_market_research import master


def prices():
    frame = pd.DataFrame(dict(Open=100., High=101., Low=99., Close=100., Volume=1000.),
                         index=pd.bdate_range('2018-09-14', '2018-09-21'))
    frame.index.name = 'Date'
    return frame


class TradingCalendarTests(unittest.TestCase):
    def test_holidays_year_end_and_special_dates(self):
        closed = pd.to_datetime(['2014-01-02', '2018-09-17', '2018-12-31', '2019-04-30',
                                 '2019-05-01', '2019-05-02', '2019-10-22', '2020-07-23',
                                 '2021-08-09', '2025-01-04'])
        opened = pd.to_datetime(['2018-09-18', '2020-07-20', '2021-08-11', '2025-12-30'])
        self.assertFalse(trading_day_mask(closed).any())
        self.assertTrue(trading_day_mask(opened).all())
        self.assertFalse(trading_day_mask(pd.to_datetime(['2018-09-16T15:00:00Z'])).any())
        with self.assertRaisesRegex(ValueError, '2014〜2025'):
            trading_day_mask(pd.to_datetime(['2026-01-05']))

    def test_zero_volume_on_open_day_is_preserved_and_source_is_unchanged(self):
        original = prices()
        original.loc['2018-09-18', 'Volume'] = 0
        before = original.copy(deep=True)
        kept, excluded = split_trading_rows(original)
        self.assertNotIn(pd.Timestamp('2018-09-17'), kept.index)
        self.assertIn(pd.Timestamp('2018-09-18'), kept.index)
        self.assertEqual(excluded.exclusion_reason.tolist(), ['public_holiday'])
        pd.testing.assert_frame_equal(original, before)

    def test_next_entry_and_holding_window_use_trading_days(self):
        frame = prices()
        frame.loc['2018-09-17', ['Open', 'High', 'Low', 'Close']] = [900., 901., 899., 900.]
        frame.loc['2018-09-19', ['Open', 'High', 'Low', 'Close']] = [110., 111., 109., 110.]
        frame, _ = split_trading_rows(frame)
        signal = pd.DataFrame(dict(ready=True, buy_signal=False, sell_signal=False,
                                   strategy_id='fixture'), index=frame.index)
        signal.loc[frame.index[0], 'buy_signal'] = True
        cfg = Config(start='2018-09-14', end='2018-09-21', commission=0, slippage=0,
                     time_exit_enabled=False)
        execution = frame.assign(LotSize=1, Split=1., Dividend=0., DividendPayDate=None)
        result = run_independent_backtest({'X':execution}, {'X':signal}, 'fixture', 'A',
                                         frame.index, {'X':'stock'}, cfg)
        entries = pd.concat([result.trades, result.positions], ignore_index=True)
        self.assertEqual(entries.entry_date.iloc[0], pd.Timestamp('2018-09-18'))
        outcomes, _, _ = fixed_horizon({'X':execution}, {'X':signal}, frame.index,
                                       pd.Series(100., index=frame.index), 1, cfg)
        selected = outcomes[outcomes.is_signal].iloc[0]
        self.assertEqual(selected.exit_date, pd.Timestamp('2018-09-19'))
        self.assertAlmostEqual(selected.net_return, .1)

    def test_prepare_filters_before_quality_validation_and_loader_checks_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root/'source'
            source.mkdir()
            frame = prices()
            frame.loc['2018-09-17', ['Open', 'High', 'Low', 'Close']] = -1.
            frame.loc['2018-09-17', 'Volume'] = 0
            frame.loc['2018-09-18', 'Volume'] = 0
            hashes = {}
            for ticker in STOCK_TICKERS:
                path = source/f'signals_{ticker}.csv'
                frame.to_csv(path, float_format='%.17g')
                hashes[path.name] = digest(path)
            (source/'collection.json').write_text(json.dumps(dict(kind='synthetic',provider='synthetic',
                retrieved_at='synthetic',status='source_collected_requires_execution_validation',hashes=hashes)),encoding='utf-8')
            output = root/'prepared'
            manifest = prepare(source, output)
            self.assertEqual(manifest['excluded_nontrading_rows'], 50)
            _, execution, inputs, _, calendar, _ = load_dataset(output,require_approved_universe=False,mode='exploratory')
            self.assertNotIn(pd.Timestamp('2018-09-17'), calendar)
            self.assertEqual(inputs['7203.T'].loc['2018-09-18', 'Volume'], 0)
            for ticker in STOCK_TICKERS:
                pd.testing.assert_frame_equal(execution[ticker][inputs[ticker].columns],inputs[ticker])
                self.assertEqual(digest(source/f'signals_{ticker}.csv'), hashes[f'signals_{ticker}.csv'])
            path = output/'excluded_nontrading_rows.csv'
            path.write_bytes(path.read_bytes()+b'\n')
            with self.assertRaisesRegex(ValueError, 'ハッシュ'):
                load_dataset(output,require_approved_universe=False,mode='exploratory')

    def test_market_batch_filters_reference_and_stock_before_indicators(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a, b, _, _ = master(root)
            work = root/'research'
            universe = freeze(a,b,work)
            (work/'cache').mkdir()
            frame = prices()
            frame.loc['2018-09-17', ['Open','High','Low','Close']] = -1.
            frame.loc['2018-09-17', 'Volume'] = 0
            assets = {}
            for ticker in ['1306.T','130A.T']:
                path = work/'cache'/f'{ticker}.csv'
                frame.to_csv(path,float_format='%.17g')
                assets[ticker] = dict(status='ready',hashes={path.name:digest(path)})
            (work/'collection.json').write_text(json.dumps(dict(kind='synthetic',universe_hash=universe['universe_hash'],assets=assets)))
            with patch('stock_screener.market_research.screening_signals', wraps=screening_signals) as shared:
                self.assertEqual(run_batch(work,limit=2,phase='entry'),2)
                for call in shared.call_args_list:
                    self.assertNotIn(pd.Timestamp('2018-09-17'),call.args[0].index)
                    self.assertNotIn(pd.Timestamp('2018-09-17'),call.args[2].index)
            for ticker in assets:
                folder = work/'results'/ticker
                status = json.loads((folder/'status.json').read_text())
                self.assertEqual(status['status'],'partial')
                self.assertEqual(len(pd.read_csv(folder/'excluded_nontrading_rows.csv')),1)
