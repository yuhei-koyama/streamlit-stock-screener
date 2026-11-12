"""F04。全日停止の例外日とカレンダー旧版の参照を検証する。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from stock_screener.backtest import Config
from stock_screener.backtest_io import load_dataset
from stock_screener.backtest_signals import screening_signals
from stock_screener.entry_exit_diagnosis import fixed_horizon
from stock_screener.exploratory_data import digest, prepare_exploratory
from stock_screener.independent_backtest import run_independent_backtest
from stock_screener.linda_versions import TV
from stock_screener.market_research import freeze, run_batch
from stock_screener.trading_calendar import (
    LEGACY_POLICY, POLICY, calendar_metadata, split_trading_rows, trading_day_mask)
from stock_screener.universe import STOCK_TICKERS, ETF_TICKERS
from tests.test_market_research import master


def exception_prices():
    frame = pd.DataFrame(dict(Open=100., High=101., Low=99., Close=100., Volume=1000.),
                         index=pd.bdate_range('2020-09-28','2020-10-07'))
    frame.index.name = 'Date'
    frame.loc['2020-10-01',['Open','High','Low','Close']] = 900.
    return frame


def old_version_dataset(root, include_exception=True):
    source = root/'source'
    source.mkdir()
    raw = exception_prices()
    if not include_exception:
        raw = raw.drop(pd.Timestamp('2020-10-01'))
    metadata = dict(kind='synthetic',provider='synthetic',retrieved_at='synthetic',
                    status='source_collected_requires_execution_validation',
                    assets=STOCK_TICKERS+ETF_TICKERS,hashes={})
    for ticker in metadata['assets']:
        path = source/f'signals_{ticker}.csv'
        raw.to_csv(path,index_label='Date')
        metadata['hashes'][path.name] = digest(path)
    (source/'collection.json').write_text(json.dumps(metadata),encoding='utf-8')
    data = root/'data'
    prepare_exploratory(source,data)
    prices, excluded = split_trading_rows(raw,policy=LEGACY_POLICY)
    for ticker in metadata['assets']:
        prices.to_csv(data/f'signals_{ticker}.csv',index_label='Date')
        prices.assign(LotSize=1,Split=1.,Dividend=0.,DividendPayDate=None).to_csv(
            data/f'execution_{ticker}.csv',index_label='Date')
    pd.DataFrame({'Date':prices.index}).to_csv(data/'calendar.csv',index=False)
    excluded.reset_index().assign(ticker='').to_csv(data/'excluded_nontrading_rows.csv',index=False)
    manifest_path = data/'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest.update(trading_calendar=calendar_metadata(LEGACY_POLICY),excluded_nontrading_rows=0,
                    exclusions_hash=digest(data/'excluded_nontrading_rows.csv'))
    manifest_path.write_text(json.dumps(manifest),encoding='utf-8')
    return data


class ExceptionalCalendarTests(unittest.TestCase):
    def test_versioned_exception_and_source_are_recorded(self):
        dates = pd.to_datetime(['2020-09-30','2020-10-01','2020-10-02'])
        self.assertEqual(trading_day_mask(dates).tolist(),[True,False,True])
        self.assertTrue(trading_day_mask(dates,policy=LEGACY_POLICY).all())
        metadata = calendar_metadata()
        self.assertEqual(metadata['policy'],POLICY)
        self.assertEqual(metadata['exceptional_closures'][0]['date'],'2020-10-01')
        self.assertIn('20201001-04.html',metadata['exceptional_closures'][0]['source_url'])
        self.assertNotIn('exceptional_closures',calendar_metadata(LEGACY_POLICY))
        metadata['exceptional_closures'][0]['date'] = '2020-10-02'
        self.assertFalse(trading_day_mask(dates)[1])

    def test_entry_holding_window_and_ratchet_effective_day_skip_halt(self):
        original = exception_prices()
        original.loc['2020-10-05',['Open','High','Low','Close']] = [110.,111.,109.,110.]
        frame, excluded = split_trading_rows(original)
        self.assertEqual(excluded.exclusion_reason.tolist(),['exchange_full_day_halt'])
        self.assertEqual(original.loc['2020-10-01','Volume'],1000.)
        signals = pd.DataFrame(dict(ready=True,buy_signal=False,sell_signal=False,strategy_id='fixture'),index=frame.index)
        signals.loc['2020-09-30','buy_signal'] = True
        execution = frame.assign(LotSize=1,Split=1.,Dividend=0.,DividendPayDate=None)
        cfg = Config(start='2020-09-28',end='2020-10-07',commission=0,slippage=0,time_exit_enabled=False)
        outcomes,_,_ = fixed_horizon({'X':execution},{'X':signals},frame.index,None,1,cfg)
        selected = outcomes[outcomes.is_signal].iloc[0]
        self.assertEqual(selected.entry_date,pd.Timestamp('2020-10-02'))
        self.assertEqual(selected.exit_date,pd.Timestamp('2020-10-05'))
        self.assertAlmostEqual(selected.net_return,.1)
        signals.loc[:,'buy_signal'] = False
        signals.loc['2020-09-29','buy_signal'] = True
        execution.loc['2020-09-30',['High','Close']] = [109.,108.]
        result = run_independent_backtest({'X':execution},{'X':signals},'fixture','D',frame.index,{'X':'stock'},cfg)
        updated = result.order_updates[result.order_updates.event.eq('update')]
        self.assertEqual(updated.effective_from.iloc[0],pd.Timestamp('2020-10-02'))
        self.assertEqual(result.trades.exit_date.iloc[0],pd.Timestamp('2020-10-02'))

    def test_old_version_can_be_read_but_all_cli_execution_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = old_version_dataset(root)
            _,_,_,_,calendar,_ = load_dataset(data,mode='exploratory')
            self.assertIn(pd.Timestamp('2020-10-01'),calendar)
            repository = Path(__file__).resolve().parents[1]
            commands = [
                ['scripts/backtest.py','--mode','exploratory'],
                ['scripts/diagnose_entry_exit.py','--fund-csv',str(root/'unused-fund.csv')],
                ['scripts/validate_additional_period.py','run','--previous',str(root/'unused-summary.csv')]]
            for index,command in enumerate(commands):
                with self.subTest(command=command[0]):
                    output = root/f'output-{index}'
                    result = subprocess.run([sys.executable,*command,'--data',str(data),'--output',str(output)],
                        cwd=repository,env={**os.environ,'PYTHONIOENCODING':'utf-8'},capture_output=True,
                        text=True,encoding='utf-8',timeout=30)
                    self.assertNotEqual(result.returncode,0)
                    self.assertIn('休場日',result.stderr)
                    self.assertFalse(output.exists())

    def test_clean_old_version_requires_new_preparation_and_unknown_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            data = old_version_dataset(Path(temp),include_exception=False)
            load_dataset(data,mode='exploratory')
            with self.assertRaisesRegex(ValueError,'新版.*再準備'):
                load_dataset(data,mode='exploratory',for_execution=True)
            path = data/'manifest.json'
            metadata = json.loads(path.read_text(encoding='utf-8'))
            metadata['trading_calendar']['policy'] = 'unknown_policy'
            path.write_text(json.dumps(metadata),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'未対応'):
                load_dataset(data,mode='exploratory')

    def test_market_batch_filters_halt_before_shared_indicators(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            listing, etfs, _, _ = master(root)
            work = root/'research'
            universe = freeze(listing,etfs,work)
            (work/'cache').mkdir()
            frame = exception_prices()
            for date in ['2016-01-04','2020-12-30','2025-12-30']:
                frame.loc[pd.Timestamp(date)] = [100.,101.,99.,100.,1000.]
            frame = frame.sort_index()
            states = {}
            for ticker in ['1306.T','130A.T']:
                path = work/'cache'/f'{ticker}.csv'
                frame.to_csv(path,index_label='Date')
                states[ticker] = dict(status='ready',hashes={path.name:digest(path)})
            (work/'collection.json').write_text(json.dumps(dict(kind='synthetic',universe_hash=universe['universe_hash'],assets=states)))
            with patch('stock_screener.market_research.STRATEGIES',[TV]),patch('stock_screener.market_research.HORIZONS',(1,)),patch('stock_screener.market_research.screening_signals',wraps=screening_signals) as shared:
                self.assertEqual(run_batch(work,limit=2,phase='entry'),2)
                for call in shared.call_args_list:
                    self.assertNotIn(pd.Timestamp('2020-10-01'),call.args[0].index)
                    self.assertNotIn(pd.Timestamp('2020-10-01'),call.args[2].index)
            for ticker in states:
                folder = work/'results'/ticker
                excluded = pd.read_csv(folder/'excluded_nontrading_rows.csv')
                self.assertEqual(excluded.exclusion_reason.tolist(),['exchange_full_day_halt'])
                status = json.loads((folder/'status.json').read_text(encoding='utf-8'))
                self.assertEqual(status['phase_records']['entry']['dependencies']['trading_calendar']['policy'],POLICY)
