"""独立仮想取引の回帰テスト。"""
from dataclasses import replace
import json
from pathlib import Path
import io
from unittest.mock import patch
import tempfile
import unittest

import pandas as pd

from stock_screener.backtest import run_backtest, Config
from stock_screener.backtest_io import run_suite, package_results
from stock_screener.backtest_signals import MINERVINI, RSI2
from stock_screener.exploratory_data import prepare_exploratory
from stock_screener.independent_backtest import run_independent_backtest
from tests.test_backtest import market, flags, config
from tests.test_exploratory import source_fixture


def independent(frame, signal, policy='A', cfg=None, strategy='fixture'):
    return run_independent_backtest(
        {'7203.T': frame}, {'7203.T': signal}, strategy, policy, frame.index,
        {'7203.T': 'stock'}, cfg or config(frame))


class IndependentTests(unittest.TestCase):
    def test_all_overlapping_signals_have_equal_notional_and_no_limits(self):
        frame = market(5)
        signal = flags(frame)
        signal.loc[frame.index[:3], 'buy_signal'] = True
        inputs = {'A': frame, 'B': frame}
        signals = {'A': signal, 'B': signal}
        classes = {'A': 'stock', 'B': 'etf'}
        tiny = replace(config(frame), initial_cash=1, allocation=.01, max_positions=1)
        large = replace(config(frame), initial_cash=1e9, allocation=1, max_positions=1000)
        a = run_independent_backtest(inputs, signals, 'fixture', 'A', frame.index, classes, tiny)
        b = run_independent_backtest(inputs, signals, 'fixture', 'A', frame.index, classes, large)
        self.assertEqual(len(a.positions), 6)
        self.assertEqual(a.positions.overlaps_existing.sum(), 4)
        self.assertTrue(a.positions.entry_cost.eq(1).all())
        self.assertEqual(len(a.rejections), 0)
        pd.testing.assert_frame_equal(a.positions, b.positions)

    def test_net_return_includes_both_fees_and_slippage(self):
        frame = market(5)
        signal = flags(frame)
        signal.loc[frame.index[0], 'buy_signal'] = True
        signal.loc[frame.index[2], 'sell_signal'] = True
        frame.loc[frame.index[3]:, ['Open', 'High', 'Low', 'Close']] = [110., 111., 109., 110.]
        cfg = replace(config(frame), commission=.001, slippage=.0005)
        result = independent(frame, signal, 'B', cfg)
        trade = result.trades.iloc[0]
        expected = (110 * (1-.0005) * (1-.001)) / (100 * (1+.0005) * (1+.001)) - 1
        self.assertAlmostEqual(trade.net_return, expected)
        self.assertEqual(trade.entry_cost, 1)
        self.assertEqual(trade.entry_date, frame.index[1])
        self.assertEqual(trade.exit_date, frame.index[3])
        self.assertAlmostEqual(result.metrics['mean_net_return'], expected)

    def test_exit_rules_match_existing_engine_for_single_signal(self):
        cases = [('A', 'fixture', 'time'), ('B', 'fixture', 'opposite_signal'),
                 ('B', RSI2, 'rsi_recovery'), ('B', MINERVINI, 'stop'),
                 ('A', 'fixture', 'gap_stop')]
        for policy, strategy, expected_reason in cases:
            with self.subTest(policy=policy, strategy=strategy, reason=expected_reason):
                frame = market(45)
                signal = flags(frame, strategy)
                signal.loc[frame.index[0], 'buy_signal'] = True
                if expected_reason == 'opposite_signal':
                    signal.loc[frame.index[2], 'sell_signal'] = True
                elif strategy == RSI2:
                    signal['rsi'] = 20.
                    signal.loc[frame.index[2]:, 'rsi'] = 50.
                elif strategy == MINERVINI:
                    frame.loc[frame.index[1], ['High', 'Low']] = [120., 95.]
                    frame.loc[frame.index[2]:, ['Open', 'High', 'Low', 'Close']] = [110., 111., 107., 110.]
                elif expected_reason == 'gap_stop':
                    frame.loc[frame.index[2]:, ['Open', 'High', 'Low', 'Close']] = [80., 81., 79., 80.]
                cfg = replace(config(frame), commission=.001, slippage=.0005)
                old = run_backtest({'7203.T': frame}, {'7203.T': signal}, strategy,
                                   policy, frame.index, {'7203.T': 'stock'}, cfg)
                new = independent(frame, signal, policy, cfg, strategy)
                self.assertEqual(len(new.trades), 1)
                a, b = old.trades.iloc[0], new.trades.iloc[0]
                self.assertEqual(b.exit_reason, expected_reason)
                for name in ('entry_date', 'exit_date', 'holding_sessions', 'exit_reason'):
                    self.assertEqual(a[name], b[name])
                for name in ('entry_price', 'exit_price', 'net_return'):
                    self.assertAlmostEqual(a[name], b[name])

    def test_untradable_entry_and_deferred_exit_and_final_signal(self):
        frame = market(7)
        signal = flags(frame)
        signal.loc[frame.index[:2], 'buy_signal'] = True
        signal.loc[frame.index[2], 'sell_signal'] = True
        signal.loc[frame.index[-1], 'buy_signal'] = True
        frame.loc[frame.index[1], 'Volume'] = 0
        frame['CanSell'] = True
        frame.loc[frame.index[3], 'CanSell'] = False
        result = independent(frame, signal, 'B')
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades.iloc[0].exit_date, frame.index[4])
        self.assertEqual(set(result.rejections.reason), {'not_tradable', 'outside_test_period'})

    def test_future_changes_preserve_completed_trades(self):
        frame = market(10)
        signal = flags(frame)
        signal.loc[frame.index[0], 'buy_signal'] = True
        signal.loc[frame.index[2], 'sell_signal'] = True
        original = independent(frame, signal, 'B')
        changed = frame.copy()
        changed.loc[frame.index[6]:, ['Open', 'High', 'Low', 'Close']] = [80., 81., 79., 80.]
        future = independent(changed, signal, 'B')
        pd.testing.assert_frame_equal(original.trades, future.trades)

    def test_empty_outputs_have_columns_and_open_marks_are_separate(self):
        frame = market(5)
        signal = flags(frame)
        empty = independent(frame, signal)
        self.assertIn('net_return', empty.trades)
        self.assertIsNone(empty.metrics['mean_net_return'])
        signal.loc[frame.index[0], 'buy_signal'] = True
        result = independent(frame, signal)
        self.assertEqual(result.metrics['trade_count'], 0)
        self.assertEqual(result.metrics['open_positions'], 1)
        self.assertEqual(result.positions.iloc[0].unrealized_return, 0)

    def test_invalid_prices_and_separate_actions_are_rejected(self):
        frame = market(5)
        signal = flags(frame)
        frame.loc[frame.index[1], 'High'] = 98.
        with self.assertRaisesRegex(ValueError, 'OHLC range'):
            independent(frame, signal)
        frame['High'] = 101.
        frame.loc[frame.index[2], 'Split'] = 2.
        with self.assertRaisesRegex(ValueError, 'embedded actions'):
            independent(frame, signal)

    def test_fourteen_conditions_save_readable_results_and_zip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_fixture(root / 'source')
            prepare_exploratory(root / 'source', root / 'data')
            cfg = Config(start='2021-01-04', end='2021-01-15')
            summary = run_suite(root / 'data', root / 'run', cfg,
                                mode='exploratory', evaluation='independent')
            self.assertEqual(len(summary), 14)
            self.assertTrue(summary.trade_count.eq(0).all())
            metadata = json.loads((root / 'run/run.json').read_text(encoding='utf-8'))
            self.assertEqual(metadata['status'], 'completed')
            self.assertEqual(metadata['evaluation_method'], 'independent')
            self.assertNotIn('initial_cash', metadata['settings'])
            self.assertIn('独立仮想取引', (root / 'run/REPORT.md').read_text(encoding='utf-8'))
            target = root / 'run' / (summary.iloc[0].strategy_id + '__A')
            self.assertIn('net_return', pd.read_csv(target / 'trades.csv'))
            self.assertIn('ticker', pd.read_csv(target / 'by_ticker.csv'))
            archive = Path(package_results(root / 'run'))
            self.assertTrue(archive.exists())
            from streamlit.testing.v1 import AppTest
            page = Path(__file__).resolve().parents[1] / 'pages' / '6_バックテスト比較.py'
            with patch('streamlit.file_uploader', return_value=io.BytesIO(archive.read_bytes())):
                view = AppTest.from_file(str(page)).run(timeout=20)
            self.assertEqual(len(view.exception), 0)
            self.assertEqual(len(view.error), 0)
            self.assertEqual(view.metric[0].label, '取引あたり平均損益率')
            with self.assertRaisesRegex(ValueError, 'requires exploratory'):
                run_suite(root / 'data', root / 'strict', cfg, evaluation='independent')
