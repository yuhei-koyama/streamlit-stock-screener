"""同額・同期間のオルカン比較の期待値と保存経路を検証する。"""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from stock_screener.backtest_io import package_results
from stock_screener.backtest_signals import STRATEGIES
from stock_screener.benchmark_comparison import (
    NAV_COLUMN, FUND_URL, compare_trades, comparison_statistics,
    grouped_comparison, load_all_country_nav, run_comparison,
)
from stock_screener.exploratory_data import digest


def trades_fixture():
    return pd.DataFrame(dict(trade_id=['trade-1'], ticker=['7203.T'], asset_class=['stock'],
                             entry_date=['2021-01-04'], exit_date=['2021-01-05'],
                             entry_cost=[1.], net_return=[.08]))


def write_nav(path, dates=None, values=None):
    frame = pd.DataFrame({'基準日': dates or ['2021/01/04', '2021/01/05'],
                          NAV_COLUMN: values or [100., 105.]})
    text = 'ｅＭＡＸＩＳ Ｓｌｉｍ 全世界株式（オール・カントリー）\n' + frame.to_csv(index=False)
    path.write_text(text, encoding='cp932')


class BenchmarkComparisonTests(unittest.TestCase):
    def test_exact_equal_notional_reference_and_percentage_point_difference(self):
        nav = pd.Series([100., 105.], index=pd.to_datetime(['2021-01-04', '2021-01-05']))
        trades = trades_fixture()
        original = trades.copy(deep=True)
        compared = compare_trades(trades, nav)
        self.assertAlmostEqual(compared.iloc[0].all_country_return, .05)
        self.assertAlmostEqual(compared.iloc[0].excess_return, .03)
        self.assertAlmostEqual(compared.iloc[0].strategy_terminal_value, 1.08)
        self.assertAlmostEqual(compared.iloc[0].all_country_terminal_value, 1.05)
        self.assertEqual(compared.iloc[0].holding_calendar_days, 1)
        pd.testing.assert_frame_equal(trades, original)

    def test_same_day_trade_and_missing_date_without_fill(self):
        nav = pd.Series([100.], index=pd.to_datetime(['2021-01-04']))
        trades = trades_fixture()
        with self.assertRaisesRegex(ValueError, 'Missing exact NAV dates'):
            compare_trades(trades, nav)
        trades['exit_date'] = '2021-01-04'
        compared = compare_trades(trades, nav)
        self.assertEqual(compared.iloc[0].all_country_return, 0)
        self.assertEqual(comparison_statistics(compared)['same_day_trades'], 1)

    def test_equal_weight_aggregation_and_groups(self):
        trades = pd.concat([trades_fixture()] * 3, ignore_index=True)
        trades['trade_id'] = ['1', '2', '3']
        trades['net_return'] = [.08, -.02, .05]
        trades['ticker'] = ['7203.T', '7203.T', '1306.T']
        nav = pd.Series([100., 105.], index=pd.to_datetime(['2021-01-04', '2021-01-05']))
        compared = compare_trades(trades, nav)
        stats = comparison_statistics(compared)
        self.assertAlmostEqual(stats['mean_excess_return'], np.mean([.03, -.07, 0]))
        self.assertAlmostEqual(stats['median_excess_return'], 0)
        self.assertAlmostEqual(stats['outperform_rate'], 1/3)
        self.assertEqual(grouped_comparison(compared, 'ticker').trade_count.sum(), 3)
        self.assertEqual(grouped_comparison(compared, 'entry_year').trade_count.sum(), 3)

    def test_official_csv_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'fund.csv'
            write_nav(path)
            self.assertEqual(load_all_country_nav(path).iloc[-1], 105.)
            for dates, values in [(['2021/01/04', '2021/01/04'], [100., 105.]),
                                   (['2021/01/05', '2021/01/04'], [100., 105.]),
                                   (['2021/01/04', '2021/01/05'], [100., 0.])]:
                write_nav(path, dates, values)
                with self.assertRaises(ValueError):
                    load_all_country_nav(path)
            path.write_text('別のファンド\n基準日,価格\n2021/01/04,100\n', encoding='cp932')
            with self.assertRaisesRegex(ValueError, 'Unexpected fund'):
                load_all_country_nav(path)

    def test_invalid_trade_inputs_and_empty_group_columns(self):
        nav = pd.Series([100., 105.], index=pd.to_datetime(['2021-01-04', '2021-01-05']))
        empty = compare_trades(trades_fixture().iloc[:0], nav)
        self.assertIsNone(comparison_statistics(empty)['mean_excess_return'])
        self.assertIn('mean_excess_return', grouped_comparison(empty, 'ticker'))
        trades = pd.concat([trades_fixture()] * 2)
        with self.assertRaisesRegex(ValueError, 'unique'):
            compare_trades(trades, nav)
        trades = trades_fixture()
        trades['entry_cost'] = 100.
        with self.assertRaisesRegex(ValueError, 'normalized'):
            compare_trades(trades, nav)

    def test_fourteen_saved_conditions_zip_and_original_preservation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source'
            source.mkdir()
            fund = root / 'fund.csv'
            metadata = root / 'fund.json'
            write_nav(fund)
            metadata.write_text(json.dumps(dict(fund_code='253425', source_url=FUND_URL,
                                                sha256=digest(fund), retrieved_at='synthetic')),
                                encoding='utf-8')
            rows = []
            for strategy in STRATEGIES:
                for policy in ('A', 'B'):
                    target = source / f'{strategy}__{policy}'
                    target.mkdir()
                    trades_fixture().to_csv(target / 'trades.csv', index=False)
                    pd.DataFrame(columns=['ticker', 'mark']).to_csv(target / 'positions.csv', index=False)
                    pd.DataFrame(columns=['ticker', 'reason']).to_csv(target / 'rejections.csv', index=False)
                    (target / 'config.json').write_text('{}')
                    rows.append(dict(strategy_id=strategy, exit_policy=policy, trade_count=1,
                                     mean_net_return=.08, open_positions=0))
            pd.DataFrame(rows).to_csv(source / 'summary.csv', index=False)
            (source / 'run.json').write_text(json.dumps(dict(
                status='completed', completed_conditions=14, evaluation_method='independent',
                execution_mode='exploratory', kind='synthetic',
                settings={'start':'2021-01-04','end':'2021-01-05'},
                code_commit='synthetic', code_dirty=False, input_hashes={})))
            originals = {path: digest(path) for path in source.rglob('*') if path.is_file()}
            output = root / 'results'
            summary = run_comparison(source, fund, metadata, output)
            self.assertEqual(len(summary), 14)
            self.assertTrue(np.isclose(summary.mean_excess_return, .03).all())
            self.assertEqual(originals, {path: digest(path) for path in originals})
            state = json.loads((output / 'run.json').read_text(encoding='utf-8'))
            self.assertEqual(state['status'], 'completed')
            self.assertEqual(state['comparison_method'], 'all_country_same_dates')
            self.assertFalse(state['annualization'])
            self.assertIn('同額・同期間比較', (output / 'REPORT.md').read_text(encoding='utf-8'))
            archive = Path(package_results(output))
            from streamlit.testing.v1 import AppTest
            page = Path(__file__).resolve().parents[1] / 'pages' / '6_バックテスト比較.py'
            with patch('streamlit.file_uploader', return_value=io.BytesIO(archive.read_bytes())):
                view = AppTest.from_file(str(page)).run(timeout=20)
            self.assertEqual(len(view.exception), 0)
            self.assertEqual(len(view.error), 0)
            self.assertEqual(view.metric[1].label, '同期間のオルカン平均損益率')
            self.assertEqual(view.metric[2].value, '+3.00ポイント')
            with self.assertRaisesRegex(ValueError, 'new output'):
                run_comparison(source, fund, metadata, output)
            all_output = root / 'including_open'
            all_summary = run_comparison(source, fund, metadata, all_output, include_open_positions=True)
            self.assertTrue(all_summary.all_trade_count.eq(1).all())
            self.assertTrue(np.isclose(all_summary.all_mean_excess_return, .03).all())
            all_archive = Path(package_results(all_output))
            with patch('streamlit.file_uploader', return_value=io.BytesIO(all_archive.read_bytes())):
                all_view = AppTest.from_file(str(page)).run(timeout=20)
            self.assertEqual(len(all_view.exception), 0)
            self.assertEqual(all_view.metric[2].value, '+3.00ポイント')
            metadata.write_text(json.dumps(dict(fund_code='253425', source_url=FUND_URL,
                                                sha256='wrong', retrieved_at='synthetic')))
            with self.assertRaisesRegex(ValueError, 'provenance/hash'):
                run_comparison(source, fund, metadata, root / 'bad')
