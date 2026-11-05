import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
from streamlit.testing.v1 import AppTest

from stock_screener.anti import evaluate_anti_310, evaluate_anti_stochastic
from stock_screener.linda_versions import (
    TV, LEGACY, ANTI_310, ANTI_STOCH, VERSIONS, evaluate_version, _tv_signals,
)

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    rng = np.random.default_rng(17)
    close = 100 + np.cumsum(rng.normal(0, 2, 180))
    return pd.DataFrame(dict(Open=close, High=close+2, Low=close-2,
                             Close=close, Volume=1000.),
                        index=pd.date_range('2024-01-01', periods=len(close)))


class VersionTests(unittest.TestCase):
    def test_tv_hand_calculation_and_smoothing(self):
        frame = pd.DataFrame(dict(High=[3.,5.,4.], Low=[1.,3.,2.], Close=[2.,4.,3.]))
        out = evaluate_version(frame, TV, fast_len=1, slow_len=2,
                               signal_len=2, k_len=2, smooth_k=1, d_len=1)
        np.testing.assert_allclose(out.fast, [0, 2/3, -1/9])
        np.testing.assert_allclose(out.slow, [0, 4/9, 2/27])
        self.assertAlmostEqual(out.k.iloc[1],75)
        self.assertAlmostEqual(out.k.iloc[2],100/3)
        self.assertEqual(out.strategy_id.iloc[-1],TV)

    def test_tv_same_bar_combination_strict_turn_and_previous_equality(self):
        frame = pd.DataFrame(dict(fast=[-1.,0.,1.], slow=[0.,0.,0.],
                                  k=[3.,1.,2.], d=[0.,1.,2.]))
        out = _tv_signals(frame.copy())
        self.assertTrue(out.buy_signal.iloc[-1])
        self.assertFalse(out.sell_signal.any())
        mirrored = _tv_signals(-frame)
        self.assertTrue(mirrored.sell_signal.iloc[-1])
        # A flat K bottom is not a Pine hook.
        frame.loc[1,'k']=3.
        self.assertFalse(_tv_signals(frame.copy()).buy_signal.any())
        # MACD crossed one bar earlier: the simultaneous AND must reject it.
        frame['k']=[3.,1.,2.]
        frame['fast']=[-1.,1.,2.]
        out = _tv_signals(frame.copy())
        self.assertTrue(out.macd_buy.iloc[1])
        self.assertTrue(out.stoch_buy_hook.iloc[2])
        self.assertFalse(out.buy_signal.any())

    def test_tv_warmup_zero_range_and_invalid_parameters(self):
        frame=fixture()
        flat=frame.assign(High=1.,Low=1.,Close=1.)
        out=evaluate_version(flat,TV)
        self.assertFalse(out.ready.any())
        self.assertFalse(out.buy_signal.any())
        self.assertFalse(out.sell_signal.any())
        for params in (dict(k_len=0),dict(fast_len=True),dict(unknown=3)):
            with self.assertRaises(ValueError):
                evaluate_version(frame,TV,**params)

    def test_tv_default_formula_and_warmup(self):
        frame=fixture()
        out=evaluate_version(frame,TV)
        expected=frame.Close.ewm(span=3,adjust=False).mean()-frame.Close.ewm(span=10,adjust=False).mean()
        np.testing.assert_allclose(out.fast,expected)
        raw=100*(frame.Close-frame.Low.rolling(7,min_periods=1).min())/(frame.High.rolling(7,min_periods=1).max()-frame.Low.rolling(7,min_periods=1).min())
        np.testing.assert_allclose(out.k,raw.rolling(3).mean(),equal_nan=True)
        np.testing.assert_allclose(out.d,raw.rolling(3).mean().rolling(10).mean(),equal_nan=True)
        self.assertFalse(out.ready.iloc[:12].any())
        self.assertTrue(out.ready.iloc[12])

    def test_tv_gap_restarts_and_future_is_causal(self):
        frame=fixture()
        frame.loc[frame.index[80],'Close']=np.nan
        out=evaluate_version(frame,TV)
        tail=evaluate_version(frame.iloc[81:],TV)
        np.testing.assert_allclose(out.fast.iloc[81:],tail.fast,equal_nan=True)
        self.assertFalse(out.loc[frame.index[80:93],'ready'].any())
        pd.testing.assert_frame_equal(out.iloc[:100],evaluate_version(frame.iloc[:100],TV))

    def test_legacy_matches_original_functions_at_every_prefix(self):
        original=runpy.run_path(str(ROOT/'tests/fixtures/legacy_original.py'))
        frame=fixture()
        out=evaluate_version(frame,LEGACY)
        ref=original['calculate_linda_macd'](frame)
        for new,old in [('fast','3/10_Oscillator'),('slow','3/10_Signal'),('histogram','3/10_Hist'),('sma16','SMA16')]:
            np.testing.assert_allclose(out[new],ref[old],equal_nan=True)
        self.assertFalse(out.ready.iloc[:29].any())
        for size in range(30,len(frame)+1):
            expected=original['check_conditions'](ref.iloc[:size])
            actual=out.iloc[size-1]
            for new,old in [('buy_signal','Anti_Buy'),('sell_signal','Anti_Sell'),('gc','3/10_GC'),('dc','3/10_DC')]:
                self.assertEqual(bool(actual[new]),bool(expected[old]),(size,new))
        self.assertGreater(int(out.buy_signal.sum()+out.sell_signal.sum()),0)
        pd.testing.assert_frame_equal(out.iloc[:90],evaluate_version(frame.iloc[:90],LEGACY))

    def test_existing_anti_flags_unchanged_and_versions_isolated(self):
        frame=fixture()
        original=frame.copy(deep=True)
        for version,fn in [(ANTI_310,evaluate_anti_310),(ANTI_STOCH,evaluate_anti_stochastic)]:
            out=evaluate_version(frame,version)
            ref=fn(frame)
            for column in ('setup_long','setup_short','hook_long','hook_short','phase','fast','slow'):
                pd.testing.assert_series_equal(out[column],ref[column])
            self.assertTrue(out.buy_signal.equals(ref.hook_long.rename('buy_signal')))
            self.assertTrue(out.sell_signal.equals(ref.hook_short.rename('sell_signal')))
        for version in VERSIONS:
            out=evaluate_version(frame,version)
            self.assertEqual(out.strategy_id.iloc[-1],version)
            self.assertTrue({'source_version','definition_version','parameter_set','decision_timestamp'}.issubset(out))
        pd.testing.assert_frame_equal(frame,original)


class VersionPageTests(unittest.TestCase):
    def test_each_page_renders_and_searches_without_real_network(self):
        pages = ['2_TradingView添付版.py','3_Anti_310適応版.py','4_Anti_7410版.py','5_初期試作版.py']
        for page in pages:
            with self.subTest(page=page), patch('stock_screener.linda_ui.fetch_daily_prices',return_value=fixture()) as fetch:
                at=AppTest.from_file(str(ROOT/'pages'/page),default_timeout=15).run()
                self.assertEqual(len(at.exception),0)
                fetch.assert_not_called()
                at.text_area[0].set_value('7203')
                # Show nonmatching rows too; a legitimate no-signal result is not an error.
                next(x for x in at.selectbox if x.label=='表示条件').select('全件（準備・不成立・データ不足を含む）')
                at.button[0].click().run()
                self.assertEqual(len(at.exception),0)
                self.assertEqual(at.dataframe[0].value.iloc[0]['Ticker'],'7203.T')
                self.assertEqual(fetch.call_count,1)
                # A UI-only filter change must reuse that page's saved result.
                next(x for x in at.selectbox if x.label=='表示条件').select('最新BUY候補のみ').run()
                self.assertEqual(len(at.exception),0)
                self.assertEqual(fetch.call_count,1)
