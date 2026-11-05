"""C/Dの価格境界・約定・翌日更新・保存を検証する。"""
from dataclasses import replace
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from stock_screener.backtest import Config
from stock_screener.backtest_io import run_suite, package_results
from stock_screener.benchmark_comparison import FUND_URL, NAV_COLUMN, run_comparison
from stock_screener.exploratory_data import prepare_exploratory, digest
from stock_screener.ratchet import initialize_ratchet, update_ratchet
from tests.test_backtest import market, flags, config
from tests.test_independent_backtest import independent
from tests.test_exploratory import source_fixture


def setup_case(n=5):
    frame = market(n)
    frame.loc[:, ['Open', 'High', 'Low', 'Close']] *= 10
    signal = flags(frame)
    signal.loc[frame.index[0], 'buy_signal'] = True
    cfg = replace(config(frame), time_exit_enabled=False)
    return frame, signal, cfg


class RatchetTests(unittest.TestCase):
    def test_levels_and_multiple_steps_and_no_downward_update(self):
        for policy, target in [('C', 1240.), ('D', 1320.)]:
            position = dict(entry_price=1000.)
            initialize_ratchet(position, .08)
            self.assertEqual((position['stop'], position['target']), (920., 1240.))
            self.assertTrue(update_ratchet(position, 1080., policy))
            self.assertEqual((position['stop'], position['target']), (1000., target))
            self.assertFalse(update_ratchet(position, 1050., policy))
        self.assertTrue(update_ratchet(position, 1400., 'D'))
        self.assertEqual((position['ratchet_step'], position['stop'], position['target']), (5, 1320., 1640.))

    def test_exact_one_r_boundary_and_fractional_entry_price(self):
        position = dict(entry_price=1000.)
        initialize_ratchet(position, .08)
        self.assertFalse(update_ratchet(position, math.nextafter(1080., -math.inf), 'D'))
        self.assertTrue(update_ratchet(position, 1080., 'D'))
        position = dict(entry_price=1000.5)
        initialize_ratchet(position, .08)
        close = position['entry_price'] + 2 * position['initial_risk']
        self.assertTrue(update_ratchet(position, close, 'D'))
        self.assertEqual(position['ratchet_step'], 2)

    def test_close_update_only_affects_next_session(self):
        frame, signal, cfg = setup_case()
        frame.loc[frame.index[1], ['Open', 'High', 'Low', 'Close']] = [1000., 1090., 950., 1080.]
        frame.loc[frame.index[2]:, ['Open', 'High', 'Low', 'Close']] = [1030., 1070., 990., 1020.]
        result = independent(frame, signal, 'D', cfg)
        self.assertEqual(result.trades.iloc[0].exit_date, frame.index[2])
        self.assertEqual(result.trades.iloc[0].exit_price, 1000.)
        update = result.order_updates.loc[result.order_updates.event.eq('update')].iloc[0]
        self.assertEqual(update.date, frame.index[1])
        self.assertEqual(update.effective_from, frame.index[2])
        self.assertEqual(update.stop, 1000.)

    def test_fixed_profit_c_and_rising_profit_d(self):
        frame, signal, cfg = setup_case(4)
        frame.loc[frame.index[1], ['Open', 'High', 'Low', 'Close']] = [1000., 1100., 990., 1080.]
        frame.loc[frame.index[2], ['Open', 'High', 'Low', 'Close']] = [1080., 1250., 1070., 1200.]
        frame.loc[frame.index[3], ['Open', 'High', 'Low', 'Close']] = [1200., 1210., 1190., 1200.]
        c = independent(frame, signal, 'C', cfg)
        d = independent(frame, signal, 'D', cfg)
        self.assertEqual(c.trades.iloc[0].exit_reason, 'take_profit')
        self.assertEqual(c.trades.iloc[0].exit_price, 1240.)
        self.assertTrue(d.trades.empty)
        self.assertEqual(d.positions.iloc[0].target, 1400.)
        self.assertEqual(d.positions.iloc[0].stop, 1080.)

    def test_gap_profit_precedes_daily_low_and_same_bar_is_flagged(self):
        frame, signal, cfg = setup_case(4)
        frame.loc[frame.index[1], ['Open', 'High', 'Low', 'Close']] = [1000., 1100., 990., 1080.]
        frame.loc[frame.index[2], ['Open', 'High', 'Low', 'Close']] = [1340., 1350., 900., 1200.]
        result = independent(frame, signal, 'D', cfg)
        self.assertEqual(result.trades.iloc[0].exit_price, 1320.)
        self.assertEqual(result.trades.iloc[0].exit_reason, 'take_profit')
        self.assertFalse(result.trades.iloc[0].ambiguous_oco)
        frame.loc[frame.index[1], ['Open', 'High', 'Low', 'Close']] = [1000., 1250., 900., 1000.]
        result = independent(frame, signal, 'C', cfg)
        self.assertEqual(result.trades.iloc[0].exit_price, 920.)
        self.assertTrue(result.trades.iloc[0].ambiguous_oco)
        self.assertEqual(result.metrics['ambiguous_oco_trades'], 1)

    def test_profit_fees_and_gap_stop_slippage(self):
        frame, signal, cfg = setup_case()
        cfg = replace(cfg, commission=.001, slippage=.0005)
        frame.loc[frame.index[1], ['Open', 'High', 'Low', 'Close']] = [1000., 1250., 990., 1100.]
        result = independent(frame, signal, 'C', cfg)
        trade = result.trades.iloc[0]
        self.assertAlmostEqual(trade.exit_price, trade.entry_price * 1.24)
        self.assertAlmostEqual(trade.net_return, 1.24 * .999 / 1.001 - 1)
        frame.loc[frame.index[1], ['Open', 'High', 'Low', 'Close']] = [1000., 1010., 990., 1000.]
        frame.loc[frame.index[2]:, ['Open', 'High', 'Low', 'Close']] = [800., 810., 790., 800.]
        result = independent(frame, signal, 'D', cfg)
        self.assertEqual(result.trades.iloc[0].exit_reason, 'gap_stop')
        self.assertAlmostEqual(result.trades.iloc[0].exit_price, 800 * .9995)

    def test_untradable_session_freezes_levels(self):
        frame, signal, cfg = setup_case(5)
        frame.loc[frame.index[1], ['Open', 'High', 'Low', 'Close']] = [1000., 1100., 990., 1080.]
        frame.loc[frame.index[2], ['Open', 'High', 'Low', 'Close']] = [1080., 1300., 900., 1200.]
        frame['CanSell'] = True
        frame.loc[frame.index[2], 'CanSell'] = False
        frame.loc[frame.index[3]:, ['Open', 'High', 'Low', 'Close']] = [1100., 1110., 990., 1050.]
        result = independent(frame, signal, 'D', cfg)
        self.assertEqual(result.trades.iloc[0].exit_price, 1000.)
        self.assertEqual(result.trades.iloc[0].exit_date, frame.index[3])
        self.assertEqual(result.metrics['order_update_count'], 1)

    def test_no_time_limit_and_future_changes(self):
        frame, signal, cfg = setup_case(45)
        result = independent(frame, signal, 'D', cfg)
        self.assertEqual(len(result.positions), 1)
        self.assertTrue(result.trades.empty)
        frame.loc[frame.index[2], 'Low'] = 900.
        original = independent(frame, signal, 'C', cfg)
        changed = frame.copy()
        changed.loc[changed.index[8]:, ['Open', 'High', 'Low', 'Close']] = [2000., 2010., 1990., 2000.]
        future = independent(changed, signal, 'C', cfg)
        pd.testing.assert_frame_equal(original.trades, future.trades)

    def test_fourteen_cd_conditions_and_benchmark_zip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_fixture(root / 'source')
            prepare_exploratory(root / 'source', root / 'data')
            cfg = Config(start='2021-01-04', end='2021-01-15', time_exit_enabled=False)
            summary = run_suite(root / 'data', root / 'run', cfg, mode='exploratory',
                                evaluation='independent', exit_set='cd')
            self.assertEqual(set(summary.exit_policy), {'C', 'D'})
            self.assertEqual(len(summary), 14)
            first = root / 'run' / f'{summary.iloc[0].strategy_id}__C'
            self.assertIn('effective_from', pd.read_csv(first / 'order_updates.csv'))
            parent = json.loads((root / 'run/run.json').read_text(encoding='utf-8'))
            self.assertEqual(parent['exit_policies'], ['C', 'D'])
            fund = root / 'fund.csv'
            frame = pd.DataFrame({'基準日': ['2021/01/04', '2021/01/15'], NAV_COLUMN: [100., 105.]})
            fund.write_text('ｅＭＡＸＩＳ Ｓｌｉｍ 全世界株式（オール・カントリー）\n'+frame.to_csv(index=False), encoding='cp932')
            metadata = root / 'source.json'
            metadata.write_text(json.dumps(dict(fund_code='253425', source_url=FUND_URL,
                                                sha256=digest(fund), retrieved_at='synthetic')))
            compared = run_comparison(root / 'run', fund, metadata, root / 'comparison', include_open_positions=True)
            self.assertEqual(set(compared.exit_policy), {'C', 'D'})
            self.assertTrue((root / 'comparison' / first.name / 'order_updates.csv').exists())
            archive = Path(package_results(root / 'comparison'))
            from streamlit.testing.v1 import AppTest
            page = Path(__file__).resolve().parents[1] / 'pages' / '6_バックテスト比較.py'
            with patch('streamlit.file_uploader', return_value=io.BytesIO(archive.read_bytes())):
                view = AppTest.from_file(str(page)).run(timeout=20)
            self.assertEqual(len(view.exception), 0)
            self.assertEqual(len(view.error), 0)
            with self.assertRaisesRegex(ValueError, 'require independent'):
                run_suite(root / 'data', root / 'bad', cfg, mode='exploratory', exit_set='cd')
