import json
from pathlib import Path
from unittest.mock import patch
import tempfile
import unittest

import pandas as pd

from scripts.validate_additional_period import prepare,valued_rows,stats
from stock_screener import correction_approval
from stock_screener.backtest_io import load_dataset
from stock_screener.exploratory_data import digest
from stock_screener.universe import STOCK_TICKERS


def synthetic_approval(source, output):
    output.mkdir(exist_ok=True)
    rows = []
    for ticker in STOCK_TICKERS:
        frame = pd.read_csv(source/f'signals_{ticker}.csv',index_col='Date',parse_dates=['Date'],float_precision='round_trip')
        bad = (frame.High < frame[['Open','Close','Low']].max(axis=1)) | (frame.Low > frame[['Open','Close','High']].min(axis=1))
        for date, row in frame.loc[bad].iterrows():
            high = float(row[['Open','High','Low','Close']].max())
            low = float(row[['Open','High','Low','Close']].min())
            rows.append(dict(ticker=ticker,Date=date,Open=row.Open,Close=row.Close,
                original_high=row.High,proposed_high=high,original_low=row.Low,proposed_low=low,
                difference=max(high-row.High,row.Low-low)))
    listing = output/'approved.csv'
    pd.DataFrame(rows).to_csv(listing,index=False,float_format='%.17g')
    record = dict(schema_version=1,approval_id='synthetic_only',approved_at='synthetic',
        source_collection_sha256=digest(source/'collection.json'),approved_list=listing.name,
        approved_list_sha256=digest(listing),approved_rows=len(rows))
    record_path = output/'approval.json'
    record_path.write_text(json.dumps(record),encoding='utf-8')
    return record_path


class AdditionalDataTests(unittest.TestCase):
    def test_rounding_requires_explicit_approval_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source';source.mkdir()
            original=pd.DataFrame(dict(Open=100.,High=101.,Low=99.,Close=100.,Volume=1000.),
                                  index=pd.bdate_range('2014-01-06',periods=10))
            original.loc[original.index[0],'High']=100.-1e-10
            hashes={}
            for ticker in STOCK_TICKERS:
                path=source/f'signals_{ticker}.csv'
                original.to_csv(path,index_label='Date',float_format='%.17g')
                hashes[path.name]=digest(path)
            meta=dict(kind='synthetic',status='source_collected_requires_execution_validation',provider='synthetic',
                      retrieved_at='synthetic',hashes=hashes)
            (source/'collection.json').write_text(json.dumps(meta),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'Unapproved'):
                prepare(source,root/'unapproved')
            self.assertFalse((root/'unapproved').exists())
            approval_path=synthetic_approval(source,root/'approval')
            with patch.multiple(correction_approval,APPROVAL_FILE=approval_path,APPROVAL_SHA256=digest(approval_path)):
                manifest=prepare(source,root/'approved',approve_rounding=True)
            self.assertEqual(manifest['rounding_rows'],50)
            _,execution,inputs,_,_,_=load_dataset(root/'approved',require_approved_universe=False,mode='exploratory')
            for ticker in STOCK_TICKERS:
                self.assertEqual(execution[ticker].High.iloc[0],100.)
                self.assertEqual(digest(source/f'signals_{ticker}.csv'),hashes[f'signals_{ticker}.csv'])
                pd.testing.assert_frame_equal(execution[ticker][['Open','High','Low','Close','Volume']],inputs[ticker])
            # A material error cannot be authorized through the rounding-only option.
            bad=source/'signals_7203.T.csv'
            changed=original.copy();changed.loc[changed.index[0],'High']=95.
            changed.to_csv(bad,index_label='Date',float_format='%.17g')
            meta['hashes'][bad.name]=digest(bad)
            (source/'collection.json').write_text(json.dumps(meta),encoding='utf-8')
            material_approval=synthetic_approval(source,root/'material-approval')
            with patch.multiple(correction_approval,APPROVAL_FILE=material_approval,APPROVAL_SHA256=digest(material_approval)):
                with self.assertRaisesRegex(ValueError,'Unapproved'):
                    prepare(source,root/'material',approve_rounding=True)

    def test_closed_and_open_returns_are_explicitly_separated(self):
        from types import SimpleNamespace
        result=SimpleNamespace(trades=pd.DataFrame(dict(net_return=[.1,-.1],holding_sessions=[5,6])),
                               positions=pd.DataFrame(dict(unrealized_return=[.2],holding_sessions=[100])))
        values=valued_rows(result)
        summary=stats(values)
        self.assertEqual(summary['all_trade_count'],3)
        self.assertEqual(summary['open_count'],1)
        self.assertAlmostEqual(summary['all_mean_strategy_return'],.2/3)
