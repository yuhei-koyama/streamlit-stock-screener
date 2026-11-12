"""F03。承認一覧・原本・補正前後の値を固定して補正を制限する。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from scripts.validate_additional_period import prepare
from stock_screener import correction_approval
from stock_screener.backtest_io import load_dataset
from stock_screener.exploratory_data import digest
from stock_screener.universe import STOCK_TICKERS
from tests.test_additional_period import synthetic_approval


class CorrectionApprovalTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = self.root/'source'
        self.source.mkdir()
        dates = pd.bdate_range('2018-09-14','2018-09-21')
        frame = pd.DataFrame(dict(Open=100.,High=101.,Low=99.,Close=100.,Volume=1000.),index=dates)
        frame.loc[dates[:2], 'High'] = 100.-1e-10
        hashes = {}
        for ticker in STOCK_TICKERS:
            path = self.source/f'signals_{ticker}.csv'
            frame.to_csv(path,index_label='Date',float_format='%.17g')
            hashes[path.name] = digest(path)
        self.metadata_path = self.source/'collection.json'
        self.metadata_path.write_text(json.dumps(dict(kind='synthetic',provider='synthetic',retrieved_at='synthetic',
            status='source_collected_requires_execution_validation',hashes=hashes)),encoding='utf-8')
        self.approval = synthetic_approval(self.source,self.root/'approval')
        self.approval_hash = digest(self.approval)
        self.listing = self.approval.parent/'approved.csv'
        self.output = self.root/'output'

    def trusted_record(self):
        return patch.multiple(correction_approval,APPROVAL_FILE=self.approval,APPROVAL_SHA256=self.approval_hash)

    def refresh_test_record(self):
        record = json.loads(self.approval.read_text(encoding='utf-8'))
        record.update(source_collection_sha256=digest(self.metadata_path),
                      approved_list_sha256=digest(self.listing),approved_rows=len(pd.read_csv(self.listing)))
        self.approval.write_text(json.dumps(record),encoding='utf-8')
        self.approval_hash = digest(self.approval)

    def update_source(self, modify):
        path = self.source/'signals_7203.T.csv'
        frame = pd.read_csv(path,index_col='Date',parse_dates=['Date'],float_precision='round_trip')
        modify(frame)
        frame.to_csv(path,index_label='Date',float_format='%.17g')
        metadata = json.loads(self.metadata_path.read_text(encoding='utf-8'))
        metadata['hashes'][path.name] = digest(path)
        self.metadata_path.write_text(json.dumps(metadata),encoding='utf-8')

    def test_flag_alone_cannot_approve_an_unlisted_source(self):
        with self.assertRaisesRegex(ValueError, '承認対象の元データコレクション'):
            prepare(self.source,self.output,approve_rounding=True)
        self.assertFalse(self.output.exists())

    def test_exact_approval_applies_trading_rows_and_accounts_for_closed_rows(self):
        before = {p:p.read_bytes() for p in self.source.iterdir()}
        with self.trusted_record():
            manifest = prepare(self.source,self.output,approve_rounding=True)
        self.assertEqual(manifest['rounding_rows'],50)
        self.assertEqual(manifest['rounding_approval']['skipped_nontrading_rows'],50)
        skipped = pd.read_csv(self.output/'skipped_approved_corrections.csv')
        self.assertTrue(skipped.reason.eq('nontrading_day').all())
        self.assertEqual((self.output/'approved_corrections.csv').read_bytes(),self.listing.read_bytes())
        for path, content in before.items():
            self.assertEqual(path.read_bytes(),content)

    def test_modified_record_or_listing_is_rejected(self):
        for path in [self.approval,self.listing]:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original+b'\n')
                with self.trusted_record():
                    with self.assertRaisesRegex(ValueError,'ハッシュ'):
                        prepare(self.source,self.output,approve_rounding=True)
                self.assertFalse(self.output.exists())
                path.write_bytes(original)

    def test_changed_source_and_updated_metadata_are_rejected(self):
        def change_volume(frame):
            frame['Volume'] = 2000.
        self.update_source(change_volume)
        with self.trusted_record():
            with self.assertRaisesRegex(ValueError,'元データコレクション'):
                prepare(self.source,self.output,approve_rounding=True)
        self.assertFalse(self.output.exists())

    def test_changed_source_without_metadata_update_is_rejected(self):
        path = self.source/'signals_7203.T.csv'
        path.write_bytes(path.read_bytes()+b'\n')
        with self.trusted_record():
            with self.assertRaisesRegex(ValueError,'元データのハッシュ'):
                prepare(self.source,self.output,approve_rounding=True)
        self.assertFalse(self.output.exists())

    def test_before_value_must_match_the_approval(self):
        rows = pd.read_csv(self.listing,float_precision='round_trip')
        rows.loc[0,'Open'] = 99.
        rows.to_csv(self.listing,index=False,float_format='%.17g')
        self.refresh_test_record()
        with self.trusted_record():
            with self.assertRaisesRegex(ValueError,'補正前価格'):
                prepare(self.source,self.output,approve_rounding=True)
        self.assertFalse(self.output.exists())

    def test_after_value_must_match_the_approval(self):
        rows = pd.read_csv(self.listing,float_precision='round_trip')
        rows['proposed_high'] = rows.proposed_high.astype(float)
        rows.loc[0,'proposed_high'] = 100.+1e-10
        rows.to_csv(self.listing,index=False,float_format='%.17g')
        self.refresh_test_record()
        with self.trusted_record():
            with self.assertRaisesRegex(ValueError,'補正後価格'):
                prepare(self.source,self.output,approve_rounding=True)
        self.assertFalse(self.output.exists())

    def test_unlisted_issue_is_rejected_even_if_source_hash_is_approved(self):
        def add_issue(frame):
            frame.loc['2018-09-18','High'] = 100.-1e-10
        self.update_source(add_issue)
        self.refresh_test_record()
        with self.trusted_record():
            with self.assertRaisesRegex(ValueError,'Unapproved'):
                prepare(self.source,self.output,approve_rounding=True)
        self.assertFalse(self.output.exists())

    def test_missing_approved_row_and_duplicate_listing_are_rejected(self):
        rows = pd.read_csv(self.listing,float_precision='round_trip')
        rows.loc[0,'Date'] = '2018-09-13'
        rows.to_csv(self.listing,index=False,float_format='%.17g')
        self.refresh_test_record()
        with self.trusted_record():
            with self.assertRaisesRegex(ValueError,'行が元データにありません'):
                prepare(self.source,self.output,approve_rounding=True)
        rows.loc[0,'Date'] = rows.loc[1,'Date']
        rows.loc[0,'ticker'] = rows.loc[1,'ticker']
        rows.to_csv(self.listing,index=False,float_format='%.17g')
        self.refresh_test_record()
        with self.trusted_record():
            with self.assertRaisesRegex(ValueError,'重複'):
                prepare(self.source,self.output,approve_rounding=True)
        self.assertFalse(self.output.exists())

    def test_prepared_approval_history_tampering_is_rejected(self):
        with self.trusted_record():
            prepare(self.source,self.output,approve_rounding=True)
        load_dataset(self.output,require_approved_universe=False,mode='exploratory')
        path = self.output/'skipped_approved_corrections.csv'
        path.write_bytes(path.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError,'承認履歴のハッシュ'):
            load_dataset(self.output,require_approved_universe=False,mode='exploratory')
