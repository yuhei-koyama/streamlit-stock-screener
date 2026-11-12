import unittest

import pandas as pd

from stock_screener.benchmark_comparison import compare_portfolio_nav


class FinitePortfolioBenchmarkTests(unittest.TestCase):
    def test_same_initial_cash_total_return_cagr_and_terminal_excess(self):
        dates = pd.DatetimeIndex(['2021-01-04', '2021-06-04', '2022-01-04'])
        daily = pd.DataFrame({'nav': [1000., 900., 1200.]}, index=dates)
        fund = pd.Series([200., 190., 220.], index=dates)
        rows, metrics = compare_portfolio_nav(daily, fund, 1000.)
        self.assertAlmostEqual(rows.all_country_nav.iloc[0], 1000.)
        self.assertAlmostEqual(metrics['benchmark_total_return'], .1)
        self.assertAlmostEqual(metrics['excess_total_return'], .1)
        self.assertAlmostEqual(metrics['excess_terminal_value'], 100.)
        self.assertLess(metrics['benchmark_max_drawdown'], 0.)
        self.assertTrue(metrics['outperformed_on_total_return'])
        self.assertEqual(metrics['benchmark_status'], 'compared')

    def test_pre_inception_period_remains_unavailable(self):
        dates = pd.DatetimeIndex(['2016-01-04', '2020-12-30'])
        daily = pd.DataFrame({'nav': [1000., 1200.]}, index=dates)
        fund = pd.Series([100., 120.], index=pd.DatetimeIndex(['2018-10-31', '2020-12-30']))
        rows, metrics = compare_portfolio_nav(daily, fund, 1000.)
        self.assertEqual(metrics['benchmark_status'], 'unavailable_before_fund_inception')
        self.assertIsNone(metrics['excess_total_return'])
        self.assertNotIn('all_country_nav', rows)

    def test_missing_exact_fund_date_is_an_error(self):
        dates = pd.date_range('2021-01-04', periods=3)
        daily = pd.DataFrame({'nav': [1000., 1100., 1200.]}, index=dates)
        fund = pd.Series([100., 120.], index=dates[[0, 2]])
        with self.assertRaisesRegex(ValueError, '同日付基準価額が欠損'):
            compare_portfolio_nav(daily, fund, 1000.)

    def test_stale_portfolio_has_arithmetic_excess_but_defers_verdict(self):
        dates = pd.date_range('2021-01-04', periods=3)
        daily = pd.DataFrame({'nav': [1000., 1100., 1200.]}, index=dates)
        fund = pd.Series([100., 105., 110.], index=dates)
        _, metrics = compare_portfolio_nav(daily, fund, 1000., 'provisional_stale_prices')
        self.assertEqual(metrics['benchmark_status'], 'provisional_stale_prices')
        self.assertIsNone(metrics['outperformed_on_total_return'])
        self.assertAlmostEqual(metrics['excess_total_return'], .1)
