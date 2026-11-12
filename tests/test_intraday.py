import unittest

import numpy as np
import pandas as pd

from stock_screener.backtest_signals import MINERVINI, RSI2, STRATEGIES
from stock_screener.intraday import IntradayConfig, intraday_signals, regular_bar_index, run_intraday_backtest
from stock_screener.linda_versions import TV


def inputs(tickers=('1111.T',), sessions=None, strategy=TV):
    days=pd.DatetimeIndex(sessions or ['2026-09-24'])
    grid=regular_bar_index(days)
    prices={t:pd.DataFrame(dict(Open=100.,High=100.1,Low=99.9,Close=100.,Volume=1000.),index=grid) for t in tickers}
    flags={t:pd.DataFrame(dict(buy_signal=False,sell_signal=False,ready=True,strategy_id=strategy,rsi=5.),index=grid) for t in tickers}
    daily={t:pd.DataFrame(dict(Close=100.,Volume=100000.),index=days) for t in tickers}
    return prices,flags,days,daily


def execute(data,policy='A',strategy=TV,window='morning_0900_0930',auxiliary=None,**settings):
    prices,flags,days,daily=data
    cfg=IntradayConfig(start=str(days[0].date()),end=str(days[-1].date()),window=window,
        **({'initial_cash':1000.,'purchase_budget':100.,'commission':0.,'slippage':0.}|settings))
    return run_intraday_backtest(prices,flags,strategy,policy,days,daily,cfg,auxiliary)


def at(time,date='2026-09-24'):
    return pd.Timestamp(date+' '+time,tz='Asia/Tokyo')


class IntradayTests(unittest.TestCase):
    def test_confirmed_signal_next_open_and_morning_close(self):
        data=inputs()
        data[1]['1111.T'].loc[at('09:00'),'buy_signal']=True
        result=execute(data)
        trade=result.trades.iloc[0]
        self.assertEqual(trade.signal_time,at('09:05'))
        self.assertEqual(trade.entry_time,at('09:05'))
        self.assertEqual(trade.exit_time,at('09:30'))
        self.assertEqual(trade.exit_reason,'window_end')
        self.assertTrue(result.positions.empty)
        self.assertTrue(result.bars.index.is_unique)

    def test_stop_exits_before_morning_deadline(self):
        data=inputs()
        data[1]['1111.T'].loc[at('09:00'),'buy_signal']=True
        data[0]['1111.T'].loc[at('09:05'),['Low','Close']]=[99.,99.4]
        result=execute(data)
        self.assertEqual(result.trades.iloc[0].exit_reason,'stop')
        self.assertLess(result.trades.iloc[0].exit_time,at('09:30'))
        self.assertAlmostEqual(result.trades.iloc[0].exit_price,99.5)
        self.assertLess(result.metrics['max_drawdown'],0)
        self.assertEqual(result.metrics['max_positions'],1)

    def test_stop_precedes_profit_when_both_touched(self):
        data=inputs()
        data[1]['1111.T'].loc[at('09:00'),'buy_signal']=True
        data[0]['1111.T'].loc[at('09:05'),['High','Low']]=[102.,99.]
        result=execute(data,'C')
        self.assertEqual(result.trades.iloc[0].exit_reason,'stop')
        self.assertTrue(result.trades.iloc[0].ambiguous_oco)

    def test_ratchet_is_effective_from_next_bar(self):
        data=inputs()
        data[1]['1111.T'].loc[at('09:00'),'buy_signal']=True
        data[0]['1111.T'].loc[at('09:05'),['Open','High','Low','Close']]=[100.,100.8,99.8,100.6]
        data[0]['1111.T'].loc[at('09:10'),['Open','High','Low','Close']]=[100.4,100.5,99.9,100.2]
        result=execute(data,'C')
        self.assertEqual(result.trades.iloc[0].exit_time,at('09:15'))
        self.assertAlmostEqual(result.trades.iloc[0].exit_price,100.)
        update=result.order_updates[result.order_updates.event.eq('ratchet_update')].iloc[0]
        self.assertEqual(update.recorded_at,at('09:10'))
        self.assertEqual(update.effective_from,at('09:10'))

    def test_D_target_moves_and_C_target_is_fixed(self):
        data=inputs()
        data[1]['1111.T'].loc[at('09:00'),'buy_signal']=True
        data[0]['1111.T'].loc[at('09:05'),['High','Low','Close']]=[100.8,99.8,100.6]
        c=execute(data,'C').order_updates
        d=execute(data,'D').order_updates
        self.assertAlmostEqual(c[c.event.eq('ratchet_update')].iloc[0].target,101.5)
        self.assertAlmostEqual(d[d.event.eq('ratchet_update')].iloc[0].target,102.)

    def test_missing_next_bar_cancels_and_does_not_retry(self):
        data=inputs()
        data[1]['1111.T'].loc[at('09:00'),'buy_signal']=True
        data[0]['1111.T'].loc[at('09:05'),:]=np.nan
        result=execute(data)
        self.assertTrue(result.trades.empty)
        self.assertTrue(result.rejections.reason.eq('next_bar_not_tradable').any())

    def test_unfilled_budget_is_not_reused_at_same_open(self):
        data=inputs(('1111.T','2222.T'))
        for flags in data[1].values():
            flags.loc[at('09:00'),'buy_signal']=True
        data[0]['1111.T'].loc[at('09:05'),'Volume']=0
        result=execute(data,initial_cash=100.)
        self.assertTrue(result.trades.empty)
        self.assertTrue(result.rejections.reason.eq('insufficient_cash').any())
        self.assertAlmostEqual(result.metrics['final_nav'],100.)

    def test_open_sale_funds_same_open_buy(self):
        data=inputs(('1111.T','2222.T'))
        data[1]['1111.T'].loc[at('09:00'),'buy_signal']=True
        data[1]['2222.T'].loc[at('09:05'),'buy_signal']=True
        data[0]['1111.T'].loc[at('09:10'),['Open','High','Low','Close']]=102.
        result=execute(data,'C',initial_cash=100.)
        second=result.trades[result.trades.ticker.eq('2222.T')].iloc[0]
        self.assertEqual(second.entry_time,at('09:10'))

    def test_B_opposite_signal_is_next_open(self):
        data=inputs()
        data[1]['1111.T'].loc[at('09:00'),'buy_signal']=True
        data[1]['1111.T'].loc[at('09:05'),'sell_signal']=True
        result=execute(data,'B')
        self.assertEqual(result.trades.iloc[0].exit_time,at('09:10'))
        self.assertEqual(result.trades.iloc[0].exit_reason,'opposite_signal')
        update=result.order_updates[result.order_updates.event.eq('exit_signal')].iloc[0]
        self.assertEqual(update.recorded_at,at('09:10'))
        self.assertEqual(update.effective_from,at('09:10'))

    def test_B_RSI_recovery(self):
        data=inputs(strategy=RSI2)
        data[1]['1111.T'].loc[at('09:00'),'buy_signal']=True
        data[1]['1111.T'].loc[at('09:05'),'rsi']=51.
        result=execute(data,'B',strategy=RSI2)
        self.assertEqual(result.trades.iloc[0].exit_reason,'rsi_recovery')

    def test_B_minervini_trailing(self):
        data=inputs(strategy=MINERVINI)
        data[1]['1111.T'].loc[at('09:00'),'buy_signal']=True
        data[0]['1111.T'].loc[at('09:05'),['High','Close']]=[101.2,101.]
        data[0]['1111.T'].loc[at('09:10'),['Open','High','Low','Close']]=[100.5,100.6,100.,100.2]
        result=execute(data,'B',strategy=MINERVINI)
        self.assertAlmostEqual(result.trades.iloc[0].exit_price,101.2*.99)
        update=result.order_updates[result.order_updates.event.eq('trailing_update')].iloc[0]
        self.assertAlmostEqual(update.stop,101.2*.99)
        self.assertEqual(update.effective_from,at('09:10'))

    def test_signal_at_cutoff_cannot_open_position(self):
        data=inputs()
        data[1]['1111.T'].loc[at('09:25'),'buy_signal']=True
        result=execute(data)
        self.assertTrue(result.trades.empty)
        self.assertEqual(result.rejections.iloc[0].reason,'outside_entry_window')

    def test_signal_while_held_does_not_reenter_after_gap_exit(self):
        data=inputs()
        data[1]['1111.T'].loc[[at('09:00'),at('09:05')],'buy_signal']=True
        data[0]['1111.T'].loc[at('09:10'),['Open','High','Low','Close']]=99.
        result=execute(data)
        self.assertEqual(len(result.trades),1)
        self.assertTrue(result.rejections.reason.eq('duplicate_position').any())

    def test_missing_terminal_price_keeps_holdings_and_stops_period(self):
        data=inputs(sessions=['2026-09-24','2026-09-25'])
        data[1]['1111.T'].loc[at('09:00'),'buy_signal']=True
        data[3]['1111.T'].loc[pd.Timestamp('2026-09-24'),'Close']=np.nan
        result=execute(data,window='full_session')
        self.assertEqual(result.metrics['run_status'],'incomplete_missing_terminal_price')
        self.assertEqual(result.metrics['evaluation_sessions'],1)
        self.assertEqual(len(result.positions),1)
        self.assertFalse(result.metrics['rank_eligible'])

    def test_morning_terminal_missing_does_not_use_later_bar(self):
        data=inputs()
        data[1]['1111.T'].loc[at('09:00'),'buy_signal']=True
        data[0]['1111.T'].loc[at('09:25'),:]=np.nan
        data[0]['1111.T'].loc[at('09:30'),['Open','High','Low','Close']]=200.
        result=execute(data)
        self.assertTrue(result.trades.empty)
        self.assertEqual(result.metrics['run_status'],'incomplete_missing_terminal_price')
        self.assertAlmostEqual(result.positions.iloc[0].mark,100.)

    def test_lunch_buy_signal_expires(self):
        data=inputs()
        data[1]['1111.T'].loc[at('11:25'),'buy_signal']=True
        result=execute(data,window='full_session')
        self.assertTrue(result.trades.empty)
        self.assertTrue(result.rejections.reason.eq('outside_entry_window').any())

    def test_closing_auction_profit_precedes_window_exit(self):
        data=inputs()
        data[1]['1111.T'].loc[at('09:00'),'buy_signal']=True
        data[3]['1111.T']['Close']=102.
        result=execute(data,'C',window='full_session')
        self.assertEqual(result.trades.iloc[0].exit_reason,'take_profit')
        self.assertAlmostEqual(result.trades.iloc[0].exit_price,101.5)

    def test_midday_auction_can_execute_existing_stop(self):
        data=inputs()
        data[1]['1111.T'].loc[at('09:00'),'buy_signal']=True
        aux={'1111.T':pd.DataFrame(dict(Open=99.,High=99.1,Low=98.9,Close=99.,Volume=100.),index=[at('11:30')])}
        result=execute(data,window='full_session',auxiliary=aux)
        self.assertEqual(result.trades.iloc[0].exit_bar_start,at('11:30'))
        self.assertEqual(result.trades.iloc[0].exit_reason,'gap_stop')

    def test_fees_and_cash_accounting(self):
        data=inputs()
        data[1]['1111.T'].loc[at('09:00'),'buy_signal']=True
        result=execute(data,commission=.001,slippage=.0005)
        trade=result.trades.iloc[0]
        self.assertAlmostEqual(trade.quantity,100/(100.05*1.001))
        self.assertAlmostEqual(trade.gross_pnl,0.)
        self.assertAlmostEqual(result.metrics['final_nav'],1000+trade.net_pnl)
        self.assertTrue(np.allclose(result.bars.nav,result.bars.cash+result.bars.market_value))

    def test_midday_trailing_update_is_logged_for_afternoon(self):
        data=inputs(strategy=MINERVINI)
        data[1]['1111.T'].loc[at('09:00'),'buy_signal']=True
        aux={'1111.T':pd.DataFrame(dict(Open=100.,High=101.,Low=100.,Close=100.5,Volume=100.),index=[at('11:30')])}
        result=execute(data,'B',strategy=MINERVINI,window='full_session',auxiliary=aux)
        update=result.order_updates[result.order_updates.event.eq('auction_trailing_update')].iloc[0]
        self.assertEqual(update.recorded_at,at('11:35'))
        self.assertEqual(update.effective_from,at('12:30'))
        self.assertAlmostEqual(update.stop,99.99)

    def test_missing_bar_restarts_shared_indicators(self):
        days=pd.bdate_range('2026-09-14',periods=6)
        grid=regular_bar_index(days)
        frame=pd.DataFrame(dict(Open=100.,High=101.,Low=99.,Close=100.,Volume=1000.),index=grid)
        frame.iloc[100]=np.nan
        flags=intraday_signals({'1111.T':frame},TV,days)['1111.T']
        self.assertFalse(flags.ready.iloc[101])
        self.assertTrue(flags.ready.iloc[140])

    def test_split_boundary_restarts_indicators(self):
        days=pd.bdate_range('2026-09-14',periods=6)
        grid=regular_bar_index(days)
        frame=pd.DataFrame(dict(Open=100.,High=101.,Low=99.,Close=100.,Volume=1000.),index=grid)
        split=days[3]
        flags=intraday_signals({'1111.T':frame},TV,days,{'1111.T':[split]})['1111.T']
        first=grid.get_loc(pd.Timestamp(str(split.date())+' 09:00',tz='Asia/Tokyo'))
        self.assertTrue(flags.ready.iloc[first-1])
        self.assertFalse(flags.ready.iloc[first])

    def test_empty_results_have_reusable_schema(self):
        result=execute(inputs())
        self.assertTrue({'ticker','entry_cost','unrealized_pnl'}.issubset(result.positions.columns))
        self.assertEqual(result.metrics['trade_count'],0)
        self.assertAlmostEqual(result.metrics['final_nav'],1000.)

    def test_terminal_trade_permission_is_respected(self):
        data=inputs()
        data[1]['1111.T'].loc[at('09:00'),'buy_signal']=True
        data[3]['1111.T']['CanSell']=False
        result=execute(data,window='full_session')
        self.assertEqual(len(result.positions),1)
        self.assertFalse(result.metrics['rank_eligible'])

    def test_latest_search_matches_full_history_at_decision_time(self):
        days=pd.bdate_range('2026-09-14',periods=6)
        grid=regular_bar_index(days)
        prices={t:pd.DataFrame(dict(Open=100.,High=101.,Low=99.,Close=100.,Volume=1000.),index=grid) for t in ['1111.T','1306.T']}
        prices['1111.T'].iloc[100]=np.nan
        cutoff=grid[280]+pd.Timedelta(5,unit='min')
        for strategy in STRATEGIES:
            with self.subTest(strategy=strategy):
                full=intraday_signals(prices,strategy,days,decision_time=cutoff)
                latest=intraday_signals(prices,strategy,days,decision_time=cutoff,latest_only=True)
                for ticker in prices:
                    pd.testing.assert_frame_equal(full[ticker].tail(1),latest[ticker])

    def test_unfinished_bar_is_not_used_in_latest_search(self):
        days=pd.bdate_range('2026-09-14',periods=6)
        grid=regular_bar_index(days)
        frame=pd.DataFrame(dict(Open=100.,High=101.,Low=99.,Close=100.,Volume=1000.),index=grid)
        cutoff=grid[280]+pd.Timedelta(3,unit='min')
        flags=intraday_signals({'1111.T':frame},TV,days,decision_time=cutoff,latest_only=True)['1111.T']
        self.assertEqual(flags.index[0],grid[279])

    def test_future_price_changes_do_not_change_past_signals(self):
        days=pd.bdate_range('2026-09-14',periods=6)
        grid=regular_bar_index(days)
        prices={t:pd.DataFrame(dict(Open=100.,High=101.,Low=99.,Close=100.,Volume=1000.),index=grid) for t in ['1111.T','1306.T']}
        changed={t:f.copy() for t,f in prices.items()}
        changed['1111.T'].iloc[280:,:4]=[110.,111.,109.,110.]
        for strategy in STRATEGIES:
            with self.subTest(strategy=strategy):
                before=intraday_signals(prices,strategy,days)['1111.T']
                after=intraday_signals(changed,strategy,days)['1111.T']
                pd.testing.assert_frame_equal(before.iloc[:280],after.iloc[:280])


if __name__=='__main__':
    unittest.main()
