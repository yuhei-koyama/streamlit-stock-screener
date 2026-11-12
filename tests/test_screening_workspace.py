import tempfile
import unittest
from unittest.mock import Mock

import numpy as np
import pandas as pd

from stock_screener.backtest_signals import STRATEGIES, MINERVINI
from stock_screener.screening_workspace import (DailyStore, SearchSettings, acquire_price_lock,
    combine_states, decision_date, exit_projection, indicator_prices, purchase_estimate,
    release_price_lock, scan_catalog, sessions)
from stock_screener.screening_workspace import evaluate_entry,required_history_years
from stock_screener.linda_versions import LEGACY,ANTI_STOCH


def prices():
    grid = sessions('2024-01-04','2026-11-11')
    value = np.linspace(100,200,len(grid))
    return pd.DataFrame(dict(Open=value,High=value+1,Low=value-1,Close=value,
                             **{'Adj Close':value},Volume=20_000_000.),index=grid)


class ScreeningWorkspaceTests(unittest.TestCase):
    def test_current_listing_history_starts_at_confirmed_date(self):
        frame = prices()
        assets = pd.DataFrame([dict(ticker='1111.T', name='再上場', asset_class='stock',
                                    lot_size=100, market_start_date='2026-11-10')])
        result, details = scan_catalog(assets, {'1111.T': frame}, frame.index[-1],
                                      SearchSettings(liquidity_enabled=False))
        self.assertEqual(result.iloc[0].state, '判定不可')
        self.assertEqual(details['1111.T']['prices'].index[0], pd.Timestamp('2026-11-10'))

    def test_three_state_combination(self):
        for values, union, intersection in [([True,None],True,None),([False,None],None,False),
                ([True,False],True,False),([True,True],True,True),([False,False],False,False),([None,None],None,None)]:
            self.assertIs(combine_states(values,'OR'),union)
            self.assertIs(combine_states(values,'AND'),intersection)

    def test_date_requires_manual_today_and_skips_holidays(self):
        self.assertEqual(decision_date('2026-11-04',now='2026-11-04').date().isoformat(),'2026-11-02')
        self.assertEqual(decision_date('2026-11-04',True,now='2026-11-04').date().isoformat(),'2026-11-04')
        self.assertEqual(decision_date('2026-11-08',now='2026-11-12').date().isoformat(),'2026-11-06')

    def test_lot_budget_includes_entry_cost(self):
        value = purchase_estimate(1000,100,100000,0,.0005)
        self.assertEqual(value['quantity'],0)
        value = purchase_estimate(1000,100,200200,.001,.0005)
        self.assertEqual(value['quantity'],100)
        self.assertIsNone(purchase_estimate(1000,None,None)['quantity'])

    def test_all_shared_entries_are_supported(self):
        frame = prices()
        assets = pd.DataFrame([dict(ticker='1111.T',name='テスト',asset_class='stock',lot_size=100)])
        for strategy in STRATEGIES:
            with self.subTest(strategy=strategy):
                result, details = scan_catalog(assets,{'1111.T':frame,'1306.T':frame},frame.index[-1],SearchSettings(strategies=[strategy]))
                self.assertIn(result.iloc[0].state,['成立','不成立'])
                self.assertIn(strategy,details['1111.T']['signals'])

    def test_additional_parameters_and_history_requirement(self):
        frame=prices()
        for strategy,parameters in [(LEGACY,dict(fast_len=5)),(ANTI_STOCH,dict(k_len=10)),
                                    (MINERVINI,dict(volume_multiplier=1.2))]:
            result=evaluate_entry(frame,strategy,parameters,frame.Close)
            self.assertTrue(result.ready.iloc[-1])
            self.assertIn('buy_signal',result)
        config=SearchSettings(strategies=[ANTI_STOCH],parameters={ANTI_STOCH:dict(k_len=500,fast_len=500,slow_len=500)})
        self.assertGreater(required_history_years(config),3)

    def test_exact_date_missing_reference_and_universe_limit(self):
        frame = prices()
        assets = pd.DataFrame([dict(ticker=t,name=t,asset_class='stock',lot_size=100) for t in ['1111.T','2222.T']])
        result,_ = scan_catalog(assets,{'1111.T':frame,'2222.T':frame},frame.index[-1],SearchSettings(strategies=[MINERVINI],maximum_candidates=1))
        self.assertEqual(result.set_index('ticker').loc['1111.T','state'],'判定不可')
        self.assertEqual(result.set_index('ticker').loc['2222.T','state'],'対象外')
        result,_ = scan_catalog(assets,{'1111.T':frame.iloc[:-1]},frame.index[-1],SearchSettings())
        self.assertTrue(result.state.eq('判定不可').all())

    def test_future_data_cannot_change_prior_result(self):
        frame = prices()
        asof = frame.index[-10]
        changed = frame.copy()
        changed.loc[changed.index>asof,['Open','High','Low','Close','Adj Close']] *= 2
        pd.testing.assert_frame_equal(indicator_prices(frame,asof),indicator_prices(changed,asof))

    def test_lot_size_is_unknown_before_source_date(self):
        frame=prices()
        assets=pd.DataFrame([dict(ticker='1111.T',name='ETF',asset_class='etf',lot_size=10,lot_known_from='2026-11-12')])
        result,_=scan_catalog(assets,{'1111.T':frame},frame.index[-1],SearchSettings(budget=100000.))
        self.assertEqual(result.iloc[0].state,'判定不可')
        self.assertIsNone(result.iloc[0].lot_size)

    def test_liquidity_off_removes_minimum_and_top_limit(self):
        frame=prices()
        thin=frame.assign(Volume=1.)
        assets=pd.DataFrame([dict(ticker=t,name=t,asset_class='stock',lot_size=100) for t in ['1111.T','2222.T']])
        values={'1111.T':frame,'2222.T':thin}
        enabled,_=scan_catalog(assets,values,frame.index[-1],SearchSettings(maximum_candidates=1))
        self.assertEqual(enabled.set_index('ticker').loc['2222.T','state'],'対象外')
        disabled,_=scan_catalog(assets,values,frame.index[-1],SearchSettings(maximum_candidates=1,liquidity_enabled=False))
        self.assertFalse(disabled.state.eq('対象外').any())
        budget,_=scan_catalog(assets,values,frame.index[-1],SearchSettings(liquidity_enabled=False,budget=1.))
        self.assertTrue(budget.state.eq('対象外').all())

    def test_liquidity_off_allows_missing_twenty_day_average(self):
        frame=prices().tail(19)
        assets=pd.DataFrame([dict(ticker='1111.T',name='テスト',asset_class='stock',lot_size=100)])
        result,_=scan_catalog(assets,{'1111.T':frame},frame.index[-1],SearchSettings(liquidity_enabled=False))
        self.assertIn(result.iloc[0].state,['成立','不成立'])
        self.assertIsNone(result.iloc[0].turnover)

    def test_c_and_d_use_eight_percent_steps(self):
        close = pd.Series([108.,116.],index=pd.date_range('2026-11-10',periods=2))
        for policy,target in [('C',124.),('D',140.)]:
            position,pnl,updates = exit_projection(100.,close,None,policy,.10,100,0,.0005)
            self.assertAlmostEqual(position['stop'],106.)
            self.assertAlmostEqual(position['target'],target)
            self.assertAlmostEqual(pnl,594.7)
            self.assertEqual(len(updates),2)

    def test_cache_reuse_and_failed_fetch_preserves_data(self):
        frame = prices().copy()
        frame.index = frame.index.tz_localize('Asia/Tokyo').tz_convert('UTC')
        with tempfile.TemporaryDirectory() as folder:
            store = DailyStore(folder)
            fetch = Mock(return_value=frame)
            actual,fetched = store.refresh('1111.T',pd.Timestamp('2026-11-11'),fetcher=fetch)
            self.assertTrue(fetched)
            self.assertEqual(actual.index[0],pd.Timestamp('2024-01-04'))
            again,fetched = store.refresh('1111.T',pd.Timestamp('2026-11-11'),fetcher=fetch)
            self.assertFalse(fetched)
            self.assertEqual(fetch.call_count,1)
            with self.assertRaises(ValueError):
                store.refresh('1111.T',pd.Timestamp('2026-11-11'),force=True,fetcher=Mock(return_value=pd.DataFrame()))
            pd.testing.assert_frame_equal(store.read('1111.T'),again,check_freq=False)

    def test_lock_releases_for_retry(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'price.lock'
            handle = acquire_price_lock(path)
            with self.assertRaises(ValueError):
                acquire_price_lock(path)
            release_price_lock(handle)
            release_price_lock(acquire_price_lock(path))

    def test_rebase_discards_incompatible_old_prefix(self):
        frame=prices()
        changed=frame.copy()
        changed.loc[:,['Open','High','Low','Close','Adj Close']] *= .5
        with tempfile.TemporaryDirectory() as folder:
            store=DailyStore(folder)
            store.refresh('1111.T',frame.index[-1],fetcher=Mock(return_value=frame))
            fetch=Mock(side_effect=[changed.tail(10),changed.tail(20)])
            updated,_=store.refresh('1111.T',frame.index[-1],force=True,fetcher=fetch)
            self.assertEqual(len(updated),20)
            self.assertEqual(fetch.call_count,2)
            pd.testing.assert_frame_equal(updated,changed.tail(20),check_freq=False)

    def test_historical_projection_exits_before_ratchet_update(self):
        frame=prices().tail(3).copy()
        frame.loc[:,['Open','High','Low','Close','Adj Close']]=[100.,101.,99.,100.,100.]
        frame.iloc[0,frame.columns.get_loc('Low')]=89.
        position,pnl,updates=exit_projection(100.,frame.Close,None,'C',.1,100,0,0,ohlc=frame)
        self.assertTrue(position['closed'])
        self.assertEqual(position['exit_reason'],'stop')
        self.assertAlmostEqual(pnl,-1000.)
        self.assertEqual(len(updates),1)

    def test_invalid_settings_fail_before_search(self):
        for options in [dict(strategies=[]),dict(maximum_candidates=1.5),dict(commission=float('nan')),dict(exit_policy='')]:
            with self.assertRaises(ValueError):
                SearchSettings(**options)


if __name__=='__main__':
    unittest.main()
