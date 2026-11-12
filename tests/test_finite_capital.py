import unittest

import pandas as pd

from stock_screener.finite_capital import FiniteCapitalConfig, drawdown_periods, run_finite_capital_backtest
from stock_screener.linda_versions import TV


def inputs(tickers=('1111.T',), periods=6):
    days = pd.DatetimeIndex(['2021-01-04', '2021-01-05', '2021-01-06',
                             '2021-01-07', '2021-01-08', '2021-01-12'][:periods])
    execution = {}
    flags = {}
    for ticker in tickers:
        execution[ticker] = pd.DataFrame(dict(Open=100., High=101., Low=99., Close=100.,
                    Volume=1000., LotSize=1, Split=1., Dividend=0., DividendPayDate=pd.NaT), index=days)
        flags[ticker] = pd.DataFrame(dict(buy_signal=False, sell_signal=False,
                                          ready=True, strategy_id=TV), index=days)
    return execution, flags, days


def run(execution, flags, days, policy='A', **settings):
    cfg = FiniteCapitalConfig(start=str(days[0].date()), end=str(days[-1].date()),
                               **dict({'initial_cash': 100., 'purchase_budget': 100.,
                                       'commission': 0., 'slippage': 0.}, **settings))
    return run_finite_capital_backtest(execution, flags, TV, policy, days,
                                       dict.fromkeys(execution, 'stock'), cfg)


class FiniteCapitalTests(unittest.TestCase):
    def test_equal_budget_fractional_quantity_costs_and_cash_priority(self):
        execution, flags, days = inputs(('3333.T', '2222.T', '1111.T'))
        for frame in flags.values():
            frame.loc[days[0], 'buy_signal'] = True
        result = run(execution, flags, days, initial_cash=250., commission=.001, slippage=.0005)
        self.assertEqual(result.daily.positions.max(), 2)
        self.assertAlmostEqual(result.daily.cash.iloc[-1], 50.)
        self.assertEqual(set(result.positions.ticker), {'1111.T', '2222.T'})
        self.assertAlmostEqual(result.positions.quantity.iloc[0], 100 / (100.05 * 1.001))
        self.assertEqual(result.rejections.iloc[0].ticker, '3333.T')
        self.assertEqual(result.rejections.iloc[0].reason, 'insufficient_cash')
        self.assertEqual(result.rejections.iloc[0].candidate_rank, 3)
        self.assertAlmostEqual(result.contributions.total_pnl.sum(), result.metrics['final_nav'] - 250)

    def test_held_signals_are_suppressed_and_sale_close_can_resignal(self):
        execution, flags, days = inputs()
        flags['1111.T']['buy_signal'] = True
        execution['1111.T'].loc[days[3], ['Open', 'High', 'Low', 'Close']] = [80., 100., 80., 100.]
        result = run(execution, flags, days)
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades.iloc[0].entry_date, days[1])
        self.assertEqual(result.trades.iloc[0].exit_date, days[3])
        # 損失後の余力不足による見送りも固定枠を維持する。
        self.assertTrue(result.positions.empty)
        self.assertGreater(result.metrics['duplicate_signals'], 0)
        self.assertTrue(result.rejections.reason.eq('insufficient_cash').any())

    def test_sale_day_close_resignal_buys_next_open_with_surplus_cash(self):
        execution, flags, days = inputs()
        flags['1111.T']['buy_signal'] = True
        execution['1111.T'].loc[days[3], ['Open', 'High', 'Low', 'Close']] = [80., 100., 80., 100.]
        result = run(execution, flags, days, initial_cash=200.)
        self.assertEqual(result.positions.iloc[0].entry_date, days[4])
        self.assertEqual(result.positions.iloc[0].signal_date, days[3])
        self.assertLessEqual(result.daily.positions.max(), 1)

    def test_open_sale_proceeds_fund_same_open_buys(self):
        execution, flags, days = inputs(('1111.T', '2222.T'))
        flags['1111.T'].loc[days[0], 'buy_signal'] = True
        flags['1111.T'].loc[days[1], 'sell_signal'] = True
        flags['2222.T'].loc[days[1], 'buy_signal'] = True
        execution['1111.T'].loc[days[2], ['Open', 'High', 'Low', 'Close']] = 110.
        result = run(execution, flags, days, policy='B')
        self.assertEqual(result.trades.iloc[0].exit_date, days[2])
        self.assertEqual(result.positions.iloc[0].ticker, '2222.T')
        self.assertEqual(result.positions.iloc[0].entry_date, days[2])
        self.assertAlmostEqual(result.daily.cash.iloc[-1], 10.)

    def test_intraday_proceeds_cannot_fund_earlier_open(self):
        execution, flags, days = inputs(('1111.T', '2222.T'))
        flags['1111.T'].loc[days[0], 'buy_signal'] = True
        flags['2222.T'].loc[days[1], 'buy_signal'] = True
        execution['1111.T'].loc[days[2], ['Open', 'High', 'Low', 'Close']] = [100., 100., 90., 95.]
        result = run(execution, flags, days)
        self.assertEqual(result.trades.iloc[0].exit_date, days[2])
        self.assertTrue(result.positions.empty)
        self.assertEqual(result.rejections.iloc[0].reason, 'insufficient_cash')

    def test_untradable_buy_is_cancelled_until_new_signal(self):
        execution, flags, days = inputs()
        flags['1111.T'].loc[days[0], 'buy_signal'] = True
        flags['1111.T'].loc[days[2], 'buy_signal'] = True
        execution['1111.T'].loc[days[1], 'Volume'] = 0.
        result = run(execution, flags, days)
        self.assertEqual(result.rejections.iloc[0].reason, 'not_tradable')
        self.assertEqual(result.positions.iloc[0].entry_date, days[3])

    def test_pending_sale_carries_to_next_tradable_open(self):
        execution, flags, days = inputs()
        flags['1111.T'].loc[days[0], 'buy_signal'] = True
        flags['1111.T'].loc[days[1], 'sell_signal'] = True
        execution['1111.T']['CanSell'] = True
        execution['1111.T'].loc[days[2:4], 'CanSell'] = False
        result = run(execution, flags, days, policy='B')
        self.assertEqual(result.trades.iloc[0].exit_date, days[4])
        self.assertEqual(result.trades.iloc[0].exit_reason, 'opposite_signal')

    def test_c_and_d_use_fixed_reference_and_next_session_updates(self):
        execution, flags, days = inputs(periods=3)
        flags['1111.T'].loc[days[0], 'buy_signal'] = True
        execution['1111.T'].loc[days[1], ['Open', 'High', 'Low', 'Close']] = [100., 109., 100., 108.]
        execution['1111.T'].loc[days[2], ['Open', 'High', 'Low', 'Close']] = [115., 125., 110., 120.]
        c = run(execution, flags, days, policy='C', initial_stop=.03)
        d = run(execution, flags, days, policy='D', initial_stop=.03)
        self.assertEqual(c.trades.iloc[0].exit_price, 124.)
        self.assertAlmostEqual(c.trades.iloc[0].initial_risk, 8.)
        self.assertTrue(d.trades.empty)
        self.assertEqual(d.positions.iloc[0].target, 140.)
        update = d.order_updates[d.order_updates.event.eq('update')].iloc[0]
        self.assertEqual(update.date, days[1])
        self.assertEqual(update.effective_from, days[2])
        self.assertAlmostEqual(update.stop, 105.)
        self.assertEqual(update.target, 132.)

    def test_ambiguous_oco_stop_has_priority(self):
        execution, flags, days = inputs(periods=3)
        flags['1111.T'].loc[days[0], 'buy_signal'] = True
        execution['1111.T'].loc[days[1], ['Open', 'High', 'Low', 'Close']] = [100., 125., 90., 100.]
        result = run(execution, flags, days, policy='C')
        self.assertEqual(result.trades.iloc[0].exit_reason, 'stop')
        self.assertTrue(result.trades.iloc[0].ambiguous_oco)
        self.assertAlmostEqual(result.trades.iloc[0].exit_price, 92.)

    def test_stale_terminal_mark_is_provisional_with_session_age(self):
        execution, flags, days = inputs(periods=3)
        flags['1111.T'].loc[days[0], 'buy_signal'] = True
        execution['1111.T'] = execution['1111.T'].drop(days[-1])
        flags['1111.T'] = flags['1111.T'].drop(days[-1])
        result = run(execution, flags, days)
        self.assertEqual(result.positions.iloc[0].mark_date, days[1])
        self.assertEqual(result.positions.iloc[0].stale_trading_sessions, 1)
        self.assertEqual(result.metrics['comparison_status'], 'pending_stale_price_review')

    def test_drawdowns_use_last_equal_peak_and_distinguish_unrecovered(self):
        nav = pd.Series([100., 100., 80., 90., 100., 95.], index=pd.bdate_range('2021-01-04', periods=6))
        result = drawdown_periods(nav)
        self.assertEqual(len(result), 2)
        self.assertEqual(result.iloc[0].peak_date, nav.index[1])
        self.assertEqual(result.iloc[0].trading_sessions, 3)
        self.assertTrue(result.iloc[0].recovered)
        self.assertFalse(result.iloc[1].recovered)
        self.assertEqual(result.iloc[1].trading_sessions, 1)

    def test_no_trades_has_complete_flat_nav(self):
        execution, flags, days = inputs()
        result = run(execution, flags, days)
        self.assertEqual(result.metrics['total_return'], 0.)
        self.assertTrue(result.drawdown_periods.empty)
        self.assertTrue(result.trades.empty)
        self.assertEqual(len(result.daily), len(days))

    def test_missing_expected_session_stops(self):
        execution, flags, days = inputs()
        for ticker in execution:
            execution[ticker] = execution[ticker].drop(days[2])
            flags[ticker] = flags[ticker].drop(days[2])
        with self.assertRaisesRegex(ValueError, '取引営業日が欠け'):
            run(execution, flags, days.delete(2))

    def test_single_trade_matches_existing_shared_exit_results(self):
        from stock_screener.backtest import Config
        from stock_screener.independent_backtest import run_independent_backtest
        execution, flags, days = inputs()
        flags['1111.T'].loc[days[0], 'buy_signal'] = True
        flags['1111.T'].loc[days[1], 'sell_signal'] = True
        execution['1111.T'].loc[days[1], ['Open', 'High', 'Low', 'Close']] = [100., 109., 100., 108.]
        execution['1111.T'].loc[days[2], ['Open', 'High', 'Low', 'Close']] = [110., 125., 108., 120.]
        execution['1111.T'].loc[days[3], ['Open', 'High', 'Low', 'Close']] = [100., 100., 90., 95.]
        cfg = Config(start=str(days[0].date()), end=str(days[-1].date()),
                     commission=.001, slippage=.0005, time_exit_enabled=False)
        for policy in ('A', 'B', 'C', 'D'):
            with self.subTest(policy=policy):
                actual = run(execution, flags, days, policy=policy, commission=.001, slippage=.0005)
                expected = run_independent_backtest(execution, flags, TV, policy, days,
                    {'1111.T': 'stock'}, cfg, ratchet_reference_stop=.08)
                self.assertEqual(len(actual.trades), 1)
                self.assertEqual(actual.trades.iloc[0].exit_date, expected.trades.iloc[0].exit_date)
                self.assertAlmostEqual(actual.trades.iloc[0].net_return, expected.trades.iloc[0].net_return)

    def test_cash_controls_positions_and_purchase_budget_stays_fixed_after_profit(self):
        tickers = tuple(f'{number}.T' for number in range(1111, 1118))
        execution, flags, days = inputs(tickers)
        for frame in flags.values():
            frame.loc[days[0], 'buy_signal'] = True
        many = run(execution, flags, days, initial_cash=700.)
        self.assertEqual(many.metrics['max_positions'], 7)
        self.assertAlmostEqual(many.daily.cash.iloc[-1], 0.)
        execution, flags, days = inputs()
        flags['1111.T'].loc[days[0], 'buy_signal'] = True
        flags['1111.T'].loc[days[1], 'sell_signal'] = True
        flags['1111.T'].loc[days[2], 'buy_signal'] = True
        execution['1111.T'].loc[days[2], ['Open', 'High', 'Low', 'Close']] = 130.
        profitable = run(execution, flags, days, policy='B')
        self.assertAlmostEqual(profitable.trades.iloc[0].net_pnl, 30.)
        self.assertEqual(profitable.positions.iloc[0].entry_date, days[3])
        self.assertAlmostEqual(profitable.positions.iloc[0].entry_cost, 100.)
        self.assertAlmostEqual(profitable.daily.cash.iloc[-1], 30.)

    def test_excluded_row_exposure_distinguishes_held_positions_and_rejected_buys(self):
        from types import SimpleNamespace
        from stock_screener.finite_capital import excluded_row_exposure
        result = SimpleNamespace(
            config={'start': '2021-01-04', 'end': '2021-01-08'},
            contributions=pd.DataFrame({'ticker': ['1111.T', '2222.T']}),
            trades=pd.DataFrame({'ticker': ['1111.T'], 'trade_id': ['closed'],
                'entry_date': ['2021-01-04'], 'exit_date': ['2021-01-07'], 'entry_cost': [100.]}),
            positions=pd.DataFrame({'ticker': ['2222.T'], 'trade_id': ['open'],
                'entry_date': ['2021-01-07'], 'entry_cost': [100.]}),
            rejections=pd.DataFrame({'ticker': ['2222.T'], 'date': ['2021-01-06'], 'reason': ['not_tradable']}))
        excluded = pd.DataFrame({'ticker': ['1111.T', '1111.T', '2222.T', '2222.T', '9999.T'],
                'Date': ['2021-01-05', '2021-01-07', '2021-01-06', '2021-01-08', '2021-01-05']})
        effects, metrics = excluded_row_exposure(result, excluded)
        self.assertEqual(metrics['excluded_price_rows_in_evaluation'], 4)
        self.assertEqual(metrics['excluded_price_rows_while_held'], 2)
        self.assertEqual(metrics['excluded_price_rows_with_buy_rejection'], 1)
        self.assertFalse(effects.loc[effects.Date.eq(pd.Timestamp('2021-01-07')), 'held_position'].iloc[0])

    def test_cash_pools_cannot_mix_stocks_and_etfs(self):
        execution, flags, days = inputs(('1111.T', '2222.T'))
        cfg = FiniteCapitalConfig(start=str(days[0].date()), end=str(days[-1].date()))
        with self.assertRaisesRegex(ValueError, '独立したポートフォリオ'):
            run_finite_capital_backtest(execution, flags, TV, 'A', days,
                                        {'1111.T': 'stock', '2222.T': 'etf'}, cfg)


if __name__ == '__main__':
    unittest.main()
