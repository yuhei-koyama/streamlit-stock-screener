"""保有期限撤廃と未決済を含む同額評価の回帰テスト。"""
from dataclasses import replace
import unittest

import pandas as pd

from stock_screener.backtest import Config
from stock_screener.backtest_signals import MINERVINI, RSI2
from stock_screener.benchmark_comparison import compare_including_open, valuation_statistics
from tests.test_backtest import market, flags, config
from tests.test_independent_backtest import independent
from tests.test_benchmark_comparison import trades_fixture


class NoTimeExitTests(unittest.TestCase):
    def test_a_holds_rising_trade_beyond_twenty_days(self):
        frame = market(45)
        for index, date in enumerate(frame.index):
            price = 100 + index
            frame.loc[date, ['Open', 'High', 'Low', 'Close']] = [price, price+1, price-1, price]
        signal = flags(frame)
        signal.loc[frame.index[0], 'buy_signal'] = True
        normal = independent(frame, signal)
        unlimited = independent(frame, signal, cfg=replace(config(frame), time_exit_enabled=False))
        self.assertEqual(normal.trades.iloc[0].exit_reason, 'time')
        self.assertEqual(len(unlimited.trades), 0)
        self.assertEqual(len(unlimited.positions), 1)
        self.assertEqual(normal.trades.iloc[0].entry_date, unlimited.positions.iloc[0].entry_date)
        self.assertEqual(normal.trades.iloc[0].quantity, unlimited.positions.iloc[0].quantity)
        self.assertAlmostEqual(unlimited.positions.iloc[0].unrealized_return, 144/101-1)

    def test_b_opposite_signal_after_old_time_limit(self):
        frame = market(40)
        signal = flags(frame)
        signal.loc[frame.index[0], 'buy_signal'] = True
        signal.loc[frame.index[25], 'sell_signal'] = True
        result = independent(frame, signal, 'B', replace(config(frame), time_exit_enabled=False))
        self.assertEqual(result.trades.iloc[0].exit_date, frame.index[26])
        self.assertEqual(result.trades.iloc[0].exit_reason, 'opposite_signal')

    def test_rsi_recovery_after_five_days_and_minervini_after_forty(self):
        frame = market(60)
        signal = flags(frame, RSI2)
        signal['rsi'] = 20.
        signal.loc[frame.index[0], 'buy_signal'] = True
        signal.loc[frame.index[10]:, 'rsi'] = 50.
        cfg = replace(config(frame), time_exit_enabled=False)
        result = independent(frame, signal, 'B', cfg, RSI2)
        self.assertEqual(result.trades.iloc[0].exit_date, frame.index[11])
        self.assertEqual(result.trades.iloc[0].exit_reason, 'rsi_recovery')
        signal = flags(frame, MINERVINI)
        signal.loc[frame.index[0], 'buy_signal'] = True
        result = independent(frame, signal, 'B', cfg, MINERVINI)
        self.assertTrue(result.trades.empty)
        self.assertEqual(len(result.positions), 1)
        frame.loc[frame.index[45], ['Open', 'High', 'Low', 'Close']] = [100., 120., 99., 100.]
        frame.loc[frame.index[46]:, ['Open', 'High', 'Low', 'Close']] = [110., 111., 107., 110.]
        result = independent(frame, signal, 'B', cfg, MINERVINI)
        self.assertEqual(result.trades.iloc[0].exit_date, frame.index[46])
        self.assertEqual(result.trades.iloc[0].exit_price, 108.)

    def test_gap_stop_is_retained_after_old_limit_and_config_is_boolean(self):
        frame = market(40)
        frame.loc[frame.index[25]:, ['Open', 'High', 'Low', 'Close']] = [90., 91., 89., 90.]
        signal = flags(frame)
        signal.loc[frame.index[0], 'buy_signal'] = True
        result = independent(frame, signal, cfg=replace(config(frame), time_exit_enabled=False))
        self.assertEqual(result.trades.iloc[0].exit_date, frame.index[25])
        self.assertEqual(result.trades.iloc[0].exit_reason, 'gap_stop')
        with self.assertRaisesRegex(ValueError, 'boolean'):
            Config(time_exit_enabled='false')

    def test_all_entered_valuation_includes_open_gain_and_stale_mark(self):
        nav = pd.Series([100., 105., 110.], index=pd.to_datetime(['2021-01-04', '2021-01-05', '2021-01-06']))
        opened = pd.DataFrame(dict(trade_id=['trade-2'], ticker=['7203.T'], asset_class=['stock'],
                                   entry_date=['2021-01-04'], entry_cost=[1.],
                                   mark_date=['2021-01-05'], unrealized_return=[.20]))
        valued = compare_including_open(trades_fixture(), opened, nav, '2021-01-06')
        stats = valuation_statistics(valued)
        self.assertEqual(stats['all_trade_count'], 2)
        self.assertAlmostEqual(stats['all_mean_strategy_return'], .14)
        self.assertAlmostEqual(stats['all_mean_benchmark_return'], .075)
        self.assertAlmostEqual(stats['all_mean_excess_return'], .065)
        self.assertEqual(stats['stale_open_positions'], 1)
        self.assertEqual(set(valued.valuation_status), {'closed', 'open'})
        with self.assertRaisesRegex(ValueError, 'Missing exact NAV dates'):
            compare_including_open(trades_fixture(), opened, nav.iloc[:2], '2021-01-06')
        opened['trade_id'] = 'trade-1'
        with self.assertRaisesRegex(ValueError, 'unique'):
            compare_including_open(trades_fixture(), opened, nav, '2021-01-06')

    def test_empty_and_all_open_valuation(self):
        nav = pd.Series([100., 110.], index=pd.to_datetime(['2021-01-04', '2021-01-05']))
        empty = compare_including_open(trades_fixture().iloc[:0], pd.DataFrame(), nav, '2021-01-05')
        self.assertEqual(valuation_statistics(empty)['all_trade_count'], 0)
        opened = pd.DataFrame(dict(trade_id=['open'], ticker=['7203.T'], asset_class=['stock'],
                                   entry_date=['2021-01-04'], entry_cost=[1.],
                                   mark_date=['2021-01-05'], unrealized_return=[.20]))
        valued = compare_including_open(trades_fixture().iloc[:0], opened, nav, '2021-01-05')
        self.assertAlmostEqual(valuation_statistics(valued)['all_mean_excess_return'], .10)
