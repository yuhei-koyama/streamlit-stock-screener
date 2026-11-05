import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from streamlit.testing.v1 import AppTest
from stock_screener.search import rsi, search_rsi, search_minervini

class SearchTests(unittest.TestCase):
    def fixture(self):
        close = np.linspace(100, 300, 280)
        return pd.DataFrame({'Close':close, 'High':close+1, 'Low':close-1, 'Volume':100.}, index=pd.date_range('2020-01-01', periods=280))

    def test_wilder_seed_and_recursion(self):
        value = rsi(pd.Series([10.,11.,10.,12.]), 2)
        self.assertTrue(pd.isna(value.iloc[1]))
        self.assertAlmostEqual(value.iloc[2],50)
        self.assertAlmostEqual(value.iloc[3],100-100/6)

    def test_flat_and_gap_reset(self):
        self.assertEqual(rsi(pd.Series([1.,1.,1.]),2).iloc[-1],50)
        self.assertTrue(pd.isna(rsi(pd.Series([1.,2.,3.,np.nan,4.,5.]),2).iloc[-1]))

    def test_rsi_filter_and_threshold(self):
        frame=self.fixture()
        frame.loc[frame.index[-3]:,'Close']=[290.,280.,270.]
        self.assertTrue(search_rsi(frame,2,30,True).iloc[-1]['match'])
        self.assertFalse(search_rsi(frame,2,0,False).iloc[-1]['match'])
        self.assertFalse(search_rsi(frame.iloc[:10],2,100,True).iloc[-1]['ready'])

    def test_template_breakout_volume_and_missing_benchmark(self):
        frame=self.fixture()
        frame.loc[frame.index[-1],['Close','High','Volume']]=[310.,311.,200.]
        benchmark=pd.Series(100.,index=frame.index)
        self.assertTrue(search_minervini(frame,benchmark).iloc[-1]['match'])
        frame.loc[frame.index[-1],'Volume']=150.
        self.assertFalse(search_minervini(frame,benchmark).iloc[-1]['match'])
        benchmark.iloc[-1]=np.nan
        self.assertFalse(search_minervini(frame,benchmark).iloc[-1]['ready'])

    def test_future_invariance(self):
        frame=self.fixture()
        base=pd.Series(100.,index=frame.index)
        for fn in (lambda x:search_rsi(x),lambda x:search_minervini(x,base)):
            pd.testing.assert_frame_equal(fn(frame.iloc[:260]),fn(frame).iloc[:260])

    def test_page_initial_render(self):
        page=Path(__file__).resolve().parents[1]/'pages'/'1_銘柄検索.py'
        at=AppTest.from_file(str(page)).run()
        self.assertEqual(len(at.exception),0)
        self.assertIn('銘柄検索',at.title[0].value)

    def test_page_search_with_fixed_fixture(self):
        page=Path(__file__).resolve().parents[1]/'pages'/'1_銘柄検索.py'
        frame=self.fixture()
        frame.loc[frame.index[-1],['Close','High','Volume']]=[310.,311.,200.]
        def fetch(ticker, period):
            if ticker == '1306.T':
                base=frame.copy()
                base['Close']=100.
                return base
            return frame
        with patch('app.fetch_daily_prices', side_effect=fetch):
            at=AppTest.from_file(str(page)).run()
            at.text_area[0].set_value('7203')
            at.button[0].click().run()
            self.assertEqual(len(at.exception),0)
            self.assertEqual(at.dataframe[0].value.iloc[0]['Ticker'],'7203.T')
            self.assertTrue(at.dataframe[0].value.iloc[0]['成立'])
            self.assertIn('rs126',at.dataframe[1].value.columns)
            at.selectbox[0].select('RSI平均回帰').run()
            at.checkbox[1].uncheck()
            at.button[0].click().run()
            self.assertEqual(len(at.exception),0)
            self.assertIn('rsi',at.dataframe[1].value.columns)
