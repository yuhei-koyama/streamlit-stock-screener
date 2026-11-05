import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import pandas as pd

from scripts.collect_backtest_source import collect
from stock_screener.backtest import Config, run_backtest
from stock_screener.backtest_io import load_dataset, run_suite, package_results
from stock_screener.exploratory_data import prepare_exploratory, digest, BASIS
from stock_screener.universe import STOCK_TICKERS, ETF_TICKERS


def source_fixture(root):
    root.mkdir()
    frame=pd.DataFrame(dict(Open=100.,High=101.,Low=99.,Close=100.,Volume=10000.,
                             Dividends=5.,**{'Stock Splits':2.}),
                        index=pd.bdate_range('2020-12-01',periods=45))
    metadata=dict(kind='synthetic',status='source_collected_requires_execution_validation',
                  provider='fixed_synthetic_prices',retrieved_at='synthetic',
                  assets=STOCK_TICKERS+ETF_TICKERS,hashes={})
    for ticker in metadata['assets']:
        path=root/f'signals_{ticker}.csv'
        frame.to_csv(path,index_label='Date')
        metadata['hashes'][path.name]=digest(path)
    (root/'collection.json').write_text(json.dumps(metadata),encoding='utf-8')
    return frame


class ExploratoryTests(unittest.TestCase):
    def test_offline_conversion_is_explicit_and_does_not_double_count_actions(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source'; data=Path(temp)/'data'
            source_fixture(source)
            with patch('yfinance.Ticker') as fetch:
                manifest=prepare_exploratory(source,data)
                fetch.assert_not_called()
            self.assertEqual(manifest['execution_price_basis'],BASIS)
            self.assertFalse(manifest['actions_verified'])
            self.assertFalse(manifest['lot_sizes_verified'])
            _,execution,inputs,classes,calendar,_=load_dataset(data,mode='exploratory')
            frame=execution['7203.T']
            self.assertTrue(frame.LotSize.eq(1).all())
            self.assertTrue(frame.Dividend.eq(0).all())
            self.assertTrue(frame.Split.eq(1).all())
            pd.testing.assert_frame_equal(frame[['Open','High','Low','Close','Volume']],inputs['7203.T'])
            signal=pd.DataFrame(dict(buy_signal=False,sell_signal=False,ready=True,strategy_id='fixture'),index=frame.index)
            signal.loc[frame.index[0],'buy_signal']=True
            cfg=Config(start=str(frame.index[0].date()),end=str(frame.index[3].date()),
                       initial_cash=5000,commission=0,slippage=0)
            result=run_backtest({'7203.T':frame},{'7203.T':signal},'fixture','A',calendar,
                                {'7203.T':classes['7203.T']},cfg)
            self.assertEqual(result.positions.iloc[0].quantity,10)
            self.assertEqual(result.metrics['final_nav'],5000)
            self.assertEqual(result.metrics['final_receivables'],0)
            with self.assertRaisesRegex(ValueError,'nominal prices'):
                load_dataset(data)  # Strict defaults must not silently loosen.
            with self.assertRaisesRegex(ValueError,'new output'):
                prepare_exploratory(source,data)

    def test_changed_cache_and_incomplete_collection_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source'; source_fixture(source)
            cache=source/'signals_7203.T.csv'
            cache.write_text(cache.read_text()+'\n')
            with self.assertRaisesRegex(ValueError,'hash mismatch'):
                prepare_exploratory(source,Path(temp)/'data')
            metadata=json.loads((source/'collection.json').read_text())
            metadata['status']='blocked'
            (source/'collection.json').write_text(json.dumps(metadata))
            with self.assertRaisesRegex(ValueError,'incomplete'):
                prepare_exploratory(source,Path(temp)/'data')

    def test_virtual_input_mismatch_and_dividend_addition_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source'; source_fixture(source)
            data=Path(temp)/'data'; prepare_exploratory(source,data)
            file=data/'execution_7203.T.csv'
            frame=pd.read_csv(file)
            frame.loc[0,'Dividend']=1.
            frame.to_csv(file,index=False)
            with self.assertRaisesRegex(ValueError,'no separate'):
                load_dataset(data,mode='exploratory')
            frame.loc[0,'Dividend']=0.
            frame.loc[0,'Close']=99.
            frame.to_csv(file,index=False)
            with self.assertRaisesRegex(ValueError,'identical'):
                load_dataset(data,mode='exploratory')

    def test_fixed_fourteen_exploratory_runs_and_zip_are_labeled(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source'; source_fixture(source)
            data=Path(temp)/'data'; prepare_exploratory(source,data)
            output=Path(temp)/'result'
            cfg=Config(start='2021-01-04',end='2021-01-15')
            progress=[]
            summary=run_suite(data,output,cfg,mode='exploratory',progress=progress.append)
            self.assertEqual(len(progress),21)
            self.assertIn('14/14',progress[-1])
            self.assertEqual(len(summary),14)
            self.assertTrue(summary.final_nav.eq(cfg.initial_cash).all())
            status=json.loads((output/'run.json').read_text())
            self.assertEqual(status['execution_mode'],'exploratory')
            self.assertEqual(status['kind'],'synthetic')
            self.assertEqual(status['execution_price_basis'],BASIS)
            self.assertTrue(any('virtual' in text for text in status['warnings']))
            path=package_results(output)
            with zipfile.ZipFile(path) as archive:
                self.assertIn('run.json',archive.namelist())
                self.assertIn('summary.csv',archive.namelist())
                self.assertNotIn('result/run.json',archive.namelist())
            with self.assertRaisesRegex(ValueError,'already exists'):
                package_results(output)
            status['status']='failed'
            (output/'run.json').write_text(json.dumps(status))
            with self.assertRaisesRegex(ValueError,'completed'):
                package_results(output)


class CollectionTests(unittest.TestCase):
    def test_failed_collection_resumes_only_missing_tickers_and_checks_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'cache'
            raw=pd.DataFrame(dict(Open=100.,High=101.,Low=99.,Close=100.,Volume=10000.,
                                   **{'Adj Close':90.}),index=pd.bdate_range('2020-12-01',periods=45))
            with patch('scripts.collect_backtest_source.yf.Ticker') as fetch:
                fetch.return_value.history.side_effect=[raw,RuntimeError('provider unavailable')]
                with self.assertRaisesRegex(RuntimeError,'unavailable'):
                    collect(output,pause=0)
                self.assertEqual(fetch.call_count,2)
            before=digest(output/'signals_7203.T.csv')
            with patch('scripts.collect_backtest_source.yf.Ticker') as fetch:
                fetch.return_value.history.return_value=raw
                completed=collect(output,resume=True,pause=0)
                self.assertEqual(fetch.call_count,69)
            self.assertEqual(completed['status'],'source_collected_requires_execution_validation')
            self.assertEqual(before,digest(output/'signals_7203.T.csv'))
            adjusted=pd.read_csv(output/'signals_7203.T.csv')
            self.assertTrue(adjusted.Close.eq(90.).all())
            with patch('scripts.collect_backtest_source.yf.Ticker') as fetch:
                collect(output,resume=True,pause=0)
                fetch.assert_not_called()
            cached=output/'source_7203.T.csv'
            cached.write_text(cached.read_text()+'\n')
            with patch('scripts.collect_backtest_source.yf.Ticker') as fetch:
                with self.assertRaisesRegex(ValueError,'changed'):
                    collect(output,resume=True,pause=0)
                fetch.assert_not_called()
