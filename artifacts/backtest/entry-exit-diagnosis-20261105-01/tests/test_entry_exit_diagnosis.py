"""承認済み診断の価格範囲・費用・基準加重を検証する。"""
import unittest
from dataclasses import replace
import pandas as pd
from stock_screener.entry_exit_diagnosis import fixed_horizon, entry_statistics, recovery_diagnostics
from stock_screener.ratchet import initialize_ratchet, update_ratchet
from tests.test_backtest import market, flags, config


class DiagnosisTests(unittest.TestCase):
    def test_fixed_horizon_costs_and_exit_open_excludes_later_extremes(self):
        frame=market(10)
        signal=flags(frame)
        signal.loc[frame.index[0],'buy_signal']=True
        frame.loc[frame.index[6],['High','Low']]=[900,1]
        cfg=replace(config(frame),commission=.001,slippage=.0005)
        nav=pd.Series(100.,index=frame.index)
        outcomes,baseline,excluded=fixed_horizon({'X':frame},{'X':signal},frame.index,nav,5,cfg)
        row=outcomes[outcomes.is_signal].iloc[0]
        self.assertEqual(row.entry_date,frame.index[1])
        self.assertEqual(row.exit_date,frame.index[6])
        self.assertAlmostEqual(row.net_return,(1-.0005)*(1-.001)/((1+.0005)*(1+.001))-1)
        self.assertAlmostEqual(row.mae,99/100.05-1)
        self.assertAlmostEqual(row.mfe,101/100.05-1)
        self.assertEqual(len(excluded),6)
        self.assertEqual(int(baseline.baseline_count.sum()),4)

    def test_ready_days_and_matched_weight(self):
        frame=market(10)
        signal=flags(frame)
        signal.loc[frame.index[[0,1]],'buy_signal']=True
        signal.loc[frame.index[2],'ready']=False
        nav=pd.Series(100.,index=frame.index)
        outcomes,baseline,excluded=fixed_horizon({'X':frame},{'X':signal},frame.index,nav,5,config(frame))
        self.assertEqual(len(outcomes),3)
        self.assertEqual(entry_statistics(outcomes[outcomes.is_signal])['timing_advantage'],0)
        self.assertEqual(baseline.baseline_count.iloc[0],3)

    def test_diagnostic_reference_unit_and_default_identity(self):
        for stop in (.03,.05,.08,.10,.12):
            position=dict(entry_price=1000.)
            initialize_ratchet(position,stop,.08)
            self.assertEqual(position['initial_risk'],80)
            self.assertEqual(position['target'],1240)
            self.assertAlmostEqual(position['stop'],1000*(1-stop))
            update_ratchet(position,1080,'D')
            self.assertAlmostEqual(position['stop'],1000*(1-stop)+80)
            self.assertEqual(position['target'],1320)
        old,new=dict(entry_price=1000.5),dict(entry_price=1000.5)
        initialize_ratchet(old,.08)
        initialize_ratchet(new,.08,.08)
        self.assertEqual(old,new)

    def test_recovery_requires_later_close_before_exit_open(self):
        frame=market(10)
        signal=flags(frame)
        signal.loc[frame.index[0],'buy_signal']=True
        nav=pd.Series(100.,index=frame.index)
        outcomes,_,_=fixed_horizon({'X':frame},{'X':signal},frame.index,nav,5,config(frame))
        trades=pd.DataFrame([dict(trade_id=f'X:{frame.index[0].date()}',entry_price=100.,exit_date=frame.index[2],exit_reason='stop')])
        frame.loc[:,'Close']=90.
        frame.loc[frame.index[2],'Close']=110.
        frame.loc[frame.index[6],'Close']=110.
        self.assertFalse(recovery_diagnostics(trades,outcomes,{'X':frame}).later_close_recovered.iloc[0])
        frame.loc[frame.index[3],'Close']=100.
        self.assertTrue(recovery_diagnostics(trades,outcomes,{'X':frame}).later_close_recovered.iloc[0])

    def test_baseline_uses_signal_composition_and_untradable_exclusions(self):
        up,down=market(12),market(12)
        for i,date in enumerate(up.index):
            up.loc[date,['Open','High','Low','Close']]=[100+i,101+i,99+i,100+i]
            down.loc[date,['Open','High','Low','Close']]=[100-i,101-i,99-i,100-i]
        one,many=flags(up),flags(down)
        one.loc[up.index[0],'buy_signal']=True
        many.loc[down.index[:3],'buy_signal']=True
        down['CanBuy']=True
        down.loc[down.index[5],'CanBuy']=False
        outcomes,baseline,excluded=fixed_horizon(
            {'UP':up,'DOWN':down},{'UP':one,'DOWN':many},up.index,
            pd.Series(100.,index=up.index),5,config(up))
        means=baseline.set_index('ticker').baseline_return
        selected=outcomes[outcomes.is_signal]
        self.assertEqual(len(selected),4)
        self.assertAlmostEqual(entry_statistics(selected)['matched_baseline_return'],
                               (means.loc['UP']+3*means.loc['DOWN'])/4)
        rejected=excluded[(excluded.ticker=='DOWN')&(excluded.signal_date==down.index[4])]
        self.assertEqual(rejected.reason.iloc[0],'entry_not_tradable')
