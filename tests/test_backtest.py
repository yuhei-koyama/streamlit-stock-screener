from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from stock_screener import linda_versions, search
from stock_screener.backtest import Config, run_backtest, performance
from stock_screener.backtest_signals import screening_signals, STRATEGIES, MINERVINI, RSI2, RSI14
from stock_screener.backtest_io import run_suite, load_dataset
from stock_screener.universe import STOCK_TICKERS, ETF_TICKERS


def market(n=30):
    dates=pd.bdate_range('2021-01-04',periods=n)
    return pd.DataFrame(dict(Open=100.,High=101.,Low=99.,Close=100.,Volume=10000.,
                             LotSize=1,Split=1.,Dividend=0.,DividendPayDate=None),index=dates)


def flags(frame,strategy='fixture'):
    return pd.DataFrame(dict(buy_signal=False,sell_signal=False,ready=True,strategy_id=strategy),index=frame.index)


def config(frame,**changes):
    return Config(start=str(frame.index[0].date()),end=str(frame.index[-1].date()),
                  initial_cash=10000,allocation=1,commission=0,slippage=0,**changes)


def simulate(frame,signal,policy='A',cfg=None,strategy='fixture'):
    return run_backtest({'7203.T':frame},{'7203.T':signal},strategy,policy,frame.index,
                        {'7203.T':'stock'},cfg or config(frame))


class EntryIdentityTests(unittest.TestCase):
    def test_every_entry_is_actual_screener_output_not_another_formula(self):
        rng=np.random.default_rng(21)
        c=100+np.cumsum(rng.normal(.1,1,300))
        frame=pd.DataFrame(dict(Open=c,High=c+2,Low=c-2,Close=c,Volume=10000.),
                           index=pd.bdate_range('2019-01-01',periods=300))
        for strategy in STRATEGIES:
            with self.subTest(strategy=strategy):
                if strategy in linda_versions.VERSIONS:
                    fn=linda_versions.evaluate_version
                    with patch.object(linda_versions,'evaluate_version',wraps=fn) as called:
                        actual=screening_signals(frame,strategy,frame.Close)
                        called.assert_called_once()
                    expected=fn(frame,strategy)
                    pd.testing.assert_series_equal(actual.buy_signal,expected.buy_signal)
                elif strategy==MINERVINI:
                    fn=search.search_minervini
                    with patch.object(search,'search_minervini',wraps=fn) as called:
                        actual=screening_signals(frame,strategy,frame.Close)
                        called.assert_called_once()
                    pd.testing.assert_series_equal(actual.buy_signal,fn(frame,frame.Close).match.rename('buy_signal'))
                else:
                    fn=search.search_rsi
                    period=2 if strategy==RSI2 else 14
                    with patch.object(search,'search_rsi',wraps=fn) as called:
                        actual=screening_signals(frame,strategy,frame.Close)
                        called.assert_called_once_with(frame,period=period,trend_filter=True)
                    pd.testing.assert_series_equal(actual.buy_signal,fn(frame,period=period).match.rename('buy_signal'))
                prefix=screening_signals(frame.iloc[:260],strategy,frame.Close.iloc[:260])
                pd.testing.assert_frame_equal(actual.iloc[:260],prefix)


class ExecutionTests(unittest.TestCase):
    def test_next_open_and_exact_common_holding_period(self):
        frame=market(24)
        signal=flags(frame)
        signal.loc[frame.index[0],'buy_signal']=True
        result=simulate(frame,signal)
        self.assertEqual(result.trades.iloc[0].entry_date,frame.index[1])
        self.assertEqual(result.trades.iloc[0].exit_date,frame.index[21])
        self.assertEqual(result.trades.iloc[0].holding_sessions,20)
        self.assertEqual(result.trades.iloc[0].exit_reason,'time')
        self.assertEqual(result.daily.iloc[0].positions,0)

    def test_costs_lot_rounding_and_no_duplicate_positions(self):
        frame=market(5)
        frame['LotSize']=2
        signal=flags(frame)
        signal['buy_signal']=True
        cfg=replace(config(frame),initial_cash=1000,allocation=.5,commission=.01,slippage=.01)
        result=simulate(frame,signal,cfg=cfg)
        self.assertEqual(result.positions.iloc[0].quantity,4)
        self.assertAlmostEqual(result.daily.iloc[-1].cash,591.96)
        self.assertAlmostEqual(result.daily.iloc[-1].nav,991.96)
        self.assertAlmostEqual(result.metrics['total_costs'],8.04)
        self.assertEqual(result.metrics['max_positions'],1)
        self.assertEqual(result.metrics['trade_count'],0)

    def test_gap_stop_is_not_filled_at_stop_price(self):
        frame=market(5)
        frame.loc[frame.index[2]:,['Open','High','Low','Close']]=[80.,81.,79.,80.]
        signal=flags(frame)
        signal.loc[frame.index[0],'buy_signal']=True
        result=simulate(frame,signal)
        self.assertEqual(result.trades.iloc[0].exit_price,80)
        self.assertEqual(result.trades.iloc[0].exit_reason,'gap_stop')
        self.assertAlmostEqual(result.metrics['final_nav'],8000)

    def test_trailing_high_is_effective_only_next_session(self):
        frame=market(5)
        frame.loc[frame.index[1],['Open','High','Low','Close']]=[100.,120.,95.,100.]
        frame.loc[frame.index[2]:,['Open','High','Low','Close']]=[110.,111.,107.,110.]
        signal=flags(frame,MINERVINI)
        signal.loc[frame.index[0],'buy_signal']=True
        result=simulate(frame,signal,'B',strategy=MINERVINI)
        self.assertEqual(result.trades.iloc[0].exit_date,frame.index[2])
        self.assertEqual(result.trades.iloc[0].exit_price,108)

    def test_opposite_signal_and_rsi_recovery_use_next_open(self):
        frame=market(8)
        signal=flags(frame)
        signal.loc[frame.index[0],'buy_signal']=True
        signal.loc[frame.index[2],'sell_signal']=True
        a=simulate(frame,signal,'A')
        b=simulate(frame,signal,'B')
        self.assertTrue(a.trades.empty)
        self.assertEqual(b.trades.iloc[0].exit_date,frame.index[3])
        self.assertEqual(b.trades.iloc[0].exit_reason,'opposite_signal')
        signal=flags(frame,RSI2)
        signal['rsi']=20.
        signal.loc[frame.index[0],'buy_signal']=True
        signal.loc[frame.index[2]:,'rsi']=50.
        result=simulate(frame,signal,'B',strategy=RSI2)
        self.assertEqual(result.trades.iloc[0].exit_date,frame.index[3])
        self.assertEqual(result.trades.iloc[0].exit_reason,'rsi_recovery')
        signal['rsi']=20.
        result=simulate(frame,signal,'B',strategy=RSI2)
        self.assertEqual(result.trades.iloc[0].exit_date,frame.index[6])

    def test_position_limit_and_deterministic_order(self):
        frame=market(4)
        signal=flags(frame)
        signal.loc[frame.index[0],'buy_signal']=True
        result=run_backtest({'B':frame,'A':frame},{'B':signal,'A':signal},'fixture','A',frame.index,
                            {'B':'etf','A':'stock'},replace(config(frame),max_positions=1,allocation=.5))
        self.assertEqual(result.positions.iloc[0].ticker,'A')
        self.assertEqual(result.rejections.iloc[0].ticker,'B')
        self.assertEqual(result.rejections.iloc[0].reason,'position_limit')
        self.assertTrue((result.daily.cash >= 0).all())

    def test_unaffordable_lot_and_halt_do_not_create_fills(self):
        frame=market(4)
        signal=flags(frame)
        signal.loc[frame.index[0],'buy_signal']=True
        frame['LotSize']=1000
        result=simulate(frame,signal)
        self.assertTrue(result.positions.empty)
        self.assertEqual(result.rejections.iloc[0].reason,'insufficient_cash_or_lot')
        frame['LotSize']=1
        frame.loc[frame.index[1],'Volume']=0
        result=simulate(frame,signal)
        self.assertTrue(result.positions.empty)
        self.assertEqual(result.rejections.iloc[0].reason,'not_tradable')

    def test_split_and_dividend_entitlement_cash_and_nav(self):
        frame=market(7)
        frame.loc[frame.index[2]:,['Open','High','Low','Close']]=[49.,50.,48.,49.]
        frame.loc[frame.index[2],'Split']=2.
        frame.loc[frame.index[2],'Dividend']=1.
        frame.loc[frame.index[2],'DividendPayDate']=str(frame.index[5].date())
        signal=flags(frame)
        signal.loc[frame.index[0],'buy_signal']=True
        signal.loc[frame.index[2],'sell_signal']=True
        result=simulate(frame,signal,'B')
        self.assertEqual(result.trades.iloc[0].quantity,200)
        self.assertAlmostEqual(result.daily.loc[frame.index[2],'nav'],10000)
        self.assertEqual(result.daily.loc[frame.index[3],'receivables'],200)
        self.assertEqual(result.daily.loc[frame.index[5],'receivables'],0)
        self.assertAlmostEqual(result.metrics['final_nav'],10000)
        self.assertAlmostEqual(result.trades.iloc[0].net_pnl,0)

    def test_deferred_exit_and_no_future_cash_for_morning_buys(self):
        frame=market(6)
        signal=flags(frame)
        signal.loc[frame.index[0],'buy_signal']=True
        signal.loc[frame.index[2],'sell_signal']=True
        frame['CanSell']=True
        frame.loc[frame.index[3],'CanSell']=False
        result=simulate(frame,signal,'B')
        self.assertEqual(result.trades.iloc[0].exit_date,frame.index[4])
        # A new candidate cannot use proceeds from an intraday stop in another asset.
        a=market(5)
        b=market(5)
        sa=flags(a); sb=flags(b)
        sa.loc[a.index[0],'buy_signal']=True
        sb.loc[b.index[1],'buy_signal']=True
        a.loc[a.index[2],'Low']=90.
        result=run_backtest({'A':a,'B':b},{'A':sa,'B':sb},'fixture','A',a.index,
                            {'A':'stock','B':'etf'},config(a))
        self.assertEqual(result.rejections.iloc[0].ticker,'B')
        self.assertEqual(result.rejections.iloc[0].reason,'insufficient_cash_or_lot')
        self.assertTrue(result.positions.empty)

    def test_future_changes_cannot_change_past_execution(self):
        frame=market(12)
        signal=flags(frame)
        signal.loc[frame.index[0],'buy_signal']=True
        full=simulate(frame,signal)
        changed=frame.copy()
        changed.loc[changed.index[8]:,['Open','High','Low','Close']]=[200.,201.,199.,200.]
        future=simulate(changed,signal)
        pd.testing.assert_frame_equal(full.daily.iloc[:8],future.daily.iloc[:8])
        short=simulate(frame.iloc[:8],signal.iloc[:8])
        pd.testing.assert_frame_equal(full.daily.iloc[:8],short.daily)

    def test_reject_unknown_corporate_actions_or_short_calendar(self):
        frame=market(5)
        signal=flags(frame)
        frame.loc[frame.index[2],'Dividend']=1
        with self.assertRaisesRegex(ValueError,'payment date'):
            simulate(frame,signal)
        frame['Dividend']=0.
        with self.assertRaisesRegex(ValueError,'full requested period'):
            simulate(frame.iloc[:-1],signal.iloc[:-1],cfg=config(frame))

    def test_gap_stop_cash_precedes_other_open_entries(self):
        a=market(5); b=market(5)
        sa=flags(a); sb=flags(b)
        sa.loc[a.index[0],'buy_signal']=True
        sb.loc[b.index[1],'buy_signal']=True
        a.loc[a.index[2]:,['Open','High','Low','Close']]=[80.,81.,79.,80.]
        result=run_backtest({'A':a,'B':b},{'A':sa,'B':sb},'fixture','A',a.index,
                            {'A':'stock','B':'etf'},config(a))
        self.assertEqual(result.trades.iloc[0].exit_reason,'gap_stop')
        self.assertEqual(result.positions.iloc[0].ticker,'B')
        self.assertEqual(result.positions.iloc[0].entry_date,a.index[2])
        self.assertAlmostEqual(result.metrics['final_nav'],8000)

    def test_tradeability_flags_and_partial_invalid_prices_are_rejected(self):
        frame=market(5); signal=flags(frame)
        frame['CanBuy']='False'
        with self.assertRaisesRegex(ValueError,'booleans'):
            simulate(frame,signal)
        frame=frame.drop(columns='CanBuy')
        frame.loc[frame.index[2],'Open']=np.nan
        frame.loc[frame.index[2],'Close']=-1
        with self.assertRaisesRegex(ValueError,'positive'):
            simulate(frame,signal)

    def test_cagr_uses_total_nav_and_actual_days(self):
        daily=pd.DataFrame(dict(nav=[1000.,500.,1100.],cash=[1000.,500.,1100.],
                                receivables=0.,costs=0.,positions=0),
                           index=pd.to_datetime(['2020-01-01','2020-06-01','2021-01-01']))
        metrics=performance(daily,pd.DataFrame(),1000)
        self.assertAlmostEqual(metrics['cagr'],1.1**(365.2425/366)-1)
        self.assertAlmostEqual(metrics['max_drawdown'],-.5)
        self.assertEqual(metrics['trade_count'],0)
        self.assertIsNone(metrics['win_rate'])


class SuiteTests(unittest.TestCase):
    def test_fixed_fourteen_conditions_and_manifest_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'data';root.mkdir()
            frame=market(45)
            frame.index=pd.bdate_range('2020-12-01',periods=45)
            frame.to_csv(root/'calendar.csv',columns=[],index_label='Date')
            assets=[]
            for ticker in STOCK_TICKERS+ETF_TICKERS:
                frame.to_csv(root/f'execution_{ticker}.csv',index_label='Date')
                frame.to_csv(root/f'signals_{ticker}.csv',index_label='Date')
                assets.append(dict(ticker=ticker,asset_class='stock' if ticker in STOCK_TICKERS else 'etf'))
            manifest=dict(kind='synthetic',provider='fixed_test_fixture',retrieved_at='synthetic',
                          execution_price_basis='nominal_unadjusted',actions_verified=True,
                          lot_sizes_verified=True,assets=assets)
            (root/'manifest.json').write_text(json.dumps(manifest))
            cfg=Config(start='2021-01-04',end='2021-01-15')
            output=Path(temp)/'results'
            summary=run_suite(root,output,cfg)
            self.assertEqual(len(summary),14)
            self.assertTrue(summary.final_nav.eq(cfg.initial_cash).all())
            status=json.loads((output/'run.json').read_text())
            self.assertEqual(status['status'],'completed')
            self.assertEqual(status['kind'],'synthetic')
            self.assertEqual(status['completed_conditions'],14)
            self.assertIn('manifest.json',status['input_hashes'])
            manifest['lot_sizes_verified']=False
            (root/'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError,'lot sizes'):
                load_dataset(root)

class BacktestPageTests(unittest.TestCase):
    def test_viewer_does_not_run_or_fetch_on_open(self):
        from streamlit.testing.v1 import AppTest
        page=Path(__file__).resolve().parents[1]/'pages'/'6_バックテスト比較.py'
        with patch('stock_screener.backtest_io.run_suite') as run:
            at=AppTest.from_file(str(page)).run()
            self.assertEqual(len(at.exception),0)
            self.assertIn('バックテスト',at.title[0].value)
            run.assert_not_called()
