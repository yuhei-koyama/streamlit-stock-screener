import json
from importlib.util import find_spec
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from stock_screener.market_research import select_assets,freeze,frozen_assets,collect,run_batch,aggregate,quality,write_json
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
    @unittest.skipUnless(find_spec('openpyxl'), 'requirements-research.txtのopenpyxlが必要')
    def test_freeze_accepts_official_xlsx_format(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _, listing, etfs = master(root)
            listing_path = root / 'jpx.xlsx'
            etf_path = root / 'etfs.xlsx'
            listing.to_excel(listing_path, index=False, engine='openpyxl')
            etfs.to_excel(etf_path, index=False, engine='openpyxl')
            frozen = freeze(listing_path, etf_path, root / 'research')
            self.assertEqual(frozen['asset_count'], 2)
            self.assertEqual(frozen['source_date'], '2026-10-31')
            self.assertEqual(set(frozen['source_hashes']), {'jpx.xlsx', 'etf.xlsx'})
            frozen_assets(root / 'research')

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

    def test_five_digit_share_classes_are_excluded_with_original_codes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, _, listing, etfs = master(root)
            classes = pd.DataFrame({
                '日付': [20261031] * 7,
                'コード': ['25935', '50765', '75505', '92015', '92025', '94345', '94346'],
                '銘柄名': ['種類株式'] * 7,
                '市場・商品区分': ['プライム（内国株式）'] * 7,
            })
            listing = pd.concat([listing, classes], ignore_index=True)
            listing_path = root / 'jpx.csv'
            etf_path = root / 'etfs.csv'
            listing.to_csv(listing_path, index=False)
            etfs.to_csv(etf_path, index=False)
            snapshot = freeze(listing_path, etf_path, root / 'research')
            self.assertEqual(snapshot['stock_count'], 1)
            _, assets = frozen_assets(root / 'research')
            self.assertEqual(set(assets.ticker), {'130A.T', '1306.T'})
            excluded = pd.read_csv(root / 'research/excluded_master.csv')
            special = excluded[excluded.ticker.isin(classes['コード'] + '.T')]
            self.assertEqual(len(special), 7)
            self.assertTrue(special.reason.str.contains('種類株式').all())
            duplicate = pd.concat([listing, classes.iloc[:1]], ignore_index=True)
            with self.assertRaisesRegex(ValueError, '重複'):
                select_assets(duplicate, etfs)

    def test_explicit_exclusions_preserve_originals_and_bind_reasons(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            listing, etfs, _, _ = master(root)
            original = listing.read_bytes()
            exclusions = root / 'exclusions.csv'
            pd.DataFrame({'ticker': ['130A.T', '9999.T'],
                          'reason': ['承認済みの履歴取得不可', 'ETN対象外の根拠']}).to_csv(exclusions, index=False)
            result = freeze(listing, etfs, root / 'research', exclusions_file=exclusions)
            self.assertEqual(result['asset_count'], 1)
            self.assertEqual(result['selection_exclusions']['removed_selected_rows'], 1)
            self.assertEqual(result['selection_exclusions']['already_excluded_rows'], 1)
            self.assertEqual(listing.read_bytes(), original)
            _, assets = frozen_assets(root / 'research')
            self.assertEqual(set(assets.ticker), {'1306.T'})
            reasons = pd.read_csv(root / 'research/excluded_master.csv').set_index('ticker').reason
            self.assertEqual(reasons['130A.T'], '承認済みの履歴取得不可')
            self.assertEqual(reasons['9999.T'], 'ETN対象外の根拠')
            audit = root / 'research/excluded_master.csv'
            audit.write_bytes(audit.read_bytes() + b'\n')
            with self.assertRaisesRegex(ValueError, '除外記録が変更'):
                frozen_assets(root / 'research')

    def test_invalid_explicit_exclusion_lists_fail_before_output(self):
        cases = [(['130A', '130A.T'], ['理由', '理由'], '重複'),
                 (['9997.T'], ['理由'], '公式一覧にない'),
                 (['130A.T'], [' '], '理由が空')]
        for tickers, reasons, error in cases:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                listing, etfs, _, _ = master(root)
                exclusions = root / 'exclusions.csv'
                pd.DataFrame({'ticker': tickers, 'reason': reasons}).to_csv(exclusions, index=False)
                with self.assertRaisesRegex(ValueError, error):
                    freeze(listing, etfs, root / 'research', exclusions_file=exclusions)
                self.assertFalse((root / 'research').exists())

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

    def test_interrupted_json_publish_preserves_previous_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'collection.json'
            write_json(path, {'ready': 1})
            with patch('pathlib.Path.replace', side_effect=OSError('interrupted')):
                with self.assertRaisesRegex(OSError, 'interrupted'):
                    write_json(path, {'ready': 2})
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'ready': 1})
            write_json(path, {'ready': 3})
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'ready': 3})

    def test_collection_can_record_data_blocks_without_excluding_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a, b, _, _ = master(root)
            work = root / 'research'
            freeze(a, b, work)
            class Response:
                status_code = 400
                text = '{"chart":{"result":null,"error":{"code":"Bad Request"}}}'
            with patch('stock_screener.market_research.requests.Session') as factory:
                get = factory.return_value.__enter__.return_value.get
                get.return_value = Response()
                result = collect(work, limit=2, pause=0, continue_on_data_error=True)
                self.assertEqual(get.call_count, 2)
                self.assertTrue(all(r['status'] == 'blocked_data' for r in result['assets'].values()))
                collect(work, limit=2, pause=0, continue_on_data_error=True)
                self.assertEqual(get.call_count, 2)
            meta, assets = frozen_assets(work)
            self.assertEqual(meta['asset_count'], 2)
            for ticker in assets.ticker:
                record = result['assets'][ticker]
                self.assertEqual(record['hashes'][ticker + '.json'], digest(work / 'cache' / (ticker + '.json')))

    def test_collection_continuation_still_stops_on_rate_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a, b, _, _ = master(root)
            work = root / 'research'
            freeze(a, b, work)
            class Response:
                status_code = 429
                text = 'rate limited'
                def raise_for_status(self):
                    raise RuntimeError('rate limited')
            with patch('stock_screener.market_research.requests.Session') as factory:
                get = factory.return_value.__enter__.return_value.get
                get.return_value = Response()
                with self.assertRaisesRegex(RuntimeError, 'rate limited'):
                    collect(work, limit=2, pause=0, continue_on_data_error=True)
                self.assertEqual(get.call_count, 1)

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
