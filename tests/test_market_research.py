import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from stock_screener.market_research import select_assets,freeze,frozen_assets,collect,run_batch,aggregate,quality
from stock_screener.exploratory_data import digest
from stock_screener.linda_versions import TV


def master(root):
    listing=pd.DataFrame({'日付':[20261031]*5,'コード':['130A','1306','9999','1234','5678'],
        '銘柄名':['国内株','ETF','ETN','REIT','外国株'],
        '市場・商品区分':['グロース（内国株式）','ETF・ETN','ETF・ETN','REIT','プライム（外国株式）']})
    etf=pd.DataFrame({'コード':['1306']})
    a=root/'jpx.csv';b=root/'etf.csv';listing.to_csv(a,index=False);etf.to_csv(b,index=False)
    return a,b,listing,etf


class MarketResearchTests(unittest.TestCase):
    def test_master_selection_excludes_etn_reit_and_foreign_stock(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);a,b,listing,etf=master(root)
            assets,excluded=select_assets(listing,etf)
            self.assertEqual(set(assets.ticker),{'130A.T','1306.T'})
            self.assertEqual(len(excluded),3)
            snapshot=freeze(a,b,root/'research')
            self.assertEqual(snapshot['asset_count'],2)
            self.assertEqual(snapshot['source_date'],'2026-10-31')
            frozen_assets(root/'research')
            bad=pd.concat([etf,pd.DataFrame({'コード':['1111']})])
            with self.assertRaisesRegex(ValueError,'対応しない'):
                select_assets(listing,bad)
            (root/'research/universe.csv').write_text('changed')
            with self.assertRaisesRegex(ValueError,'変更'):
                frozen_assets(root/'research')

    def test_collection_limit_resume_and_error_are_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);a,b,_,_=master(root);work=root/'research';freeze(a,b,work)
            payload=dict(chart=dict(error=None,result=[dict(timestamp=[1451862000,1767058800],
                indicators=dict(quote=[dict(open=[100.,100.],high=[101.,101.],low=[99.,99.],close=[100.,100.],volume=[1000,1000])],
                                adjclose=[dict(adjclose=[100.,100.])]))]))
            class Response:
                status_code=200
                text=json.dumps(payload)
                def json(self):return payload
                def raise_for_status(self):return None
            with patch('stock_screener.market_research.requests.Session') as factory:
                get=factory.return_value.__enter__.return_value.get
                get.return_value=Response()
                collect(work,limit=1,pause=0)
                self.assertEqual(get.call_count,1)
                collect(work,limit=1,pause=0)
                self.assertEqual(get.call_count,2)
                collect(work,limit=1,pause=0)
                self.assertEqual(get.call_count,2)
            other=root/'failed';freeze(a,b,other)
            with patch('stock_screener.market_research.requests.Session') as factory:
                get=factory.return_value.__enter__.return_value.get
                get.side_effect=RuntimeError('rate limited')
                with self.assertRaisesRegex(RuntimeError,'rate limited'):
                    collect(other,limit=100,pause=0)
                self.assertEqual(get.call_count,1)
            result=aggregate(work)
            self.assertEqual(result['status'],'partial')
            self.assertEqual(result['completed'],0)

    def test_streamed_entry_and_exit_phases_preserve_search_and_partial_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);a,b,_,_=master(root);work=root/'research';freeze(a,b,work)
            meta,assets=frozen_assets(work);cache=work/'cache';cache.mkdir()
            idx=pd.bdate_range('2015-01-05',periods=30).union(pd.bdate_range('2016-01-04',periods=30))
            idx=idx.union(pd.bdate_range('2019-01-04',periods=30)).union(pd.bdate_range('2021-01-04',periods=30))
            idx=idx.union(pd.DatetimeIndex(['2020-12-30','2025-12-30'])).sort_values()
            prices=pd.DataFrame(dict(Open=100.,High=101.,Low=99.,Close=100.,Volume=1000.),index=idx)
            states={}
            for ticker in assets.ticker:
                file=cache/f'{ticker}.csv';prices.to_csv(file,index_label='Date')
                states[ticker]=dict(status='ready',hashes={file.name:digest(file)})
            (work/'collection.json').write_text(json.dumps(dict(kind='synthetic',universe_hash=meta['universe_hash'],assets=states)))
            from stock_screener.backtest_signals import screening_signals
            with patch('stock_screener.market_research.STRATEGIES',[TV]),patch('stock_screener.market_research.HORIZONS',(5,)),patch('stock_screener.market_research.STOPS',(.08,)),patch('stock_screener.market_research.screening_signals',wraps=screening_signals) as shared:
                self.assertEqual(run_batch(work,limit=1),1)
                self.assertEqual(shared.call_count,2)
                self.assertEqual(aggregate(work)['status'],'partial')
                self.assertEqual(run_batch(work,limit=1),1)
                self.assertEqual(run_batch(work,limit=1),0)
                self.assertEqual(aggregate(work)['status'],'completed')
                self.assertEqual(aggregate(work,'exits')['status'],'partial')
                self.assertEqual(run_batch(work,limit=2,phase='exits'),2)
                self.assertEqual(aggregate(work,'exits')['status'],'completed')
            self.assertEqual(aggregate(work)['kind'],'synthetic')
            summary=pd.read_csv(work/'summary_entry/entry_summary.csv')
            self.assertEqual(set(summary.scope),{'stock','etf'})
            self.assertTrue((work/'results/130A.T/entry.zip').exists())
            self.assertTrue((work/'results/130A.T/exits.zip').exists())
            self.assertFalse(quality(prices).any().any())
            prices.iloc[0,prices.columns.get_loc('High')]=90
            self.assertEqual(len(quality(prices)),1)
            cached=work/'cache/130A.T.csv'
            cached.write_text(cached.read_text()+'\n')
            with self.assertRaisesRegex(ValueError,'価格キャッシュ'):
                aggregate(work)
