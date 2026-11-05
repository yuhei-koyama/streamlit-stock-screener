"""承認対象行に限定した価格範囲補正を検証する。"""
import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from stock_screener.backtest_io import load_dataset
from stock_screener.data_quality import correct_exploratory_data
from stock_screener.exploratory_data import prepare_exploratory, digest
from tests.test_exploratory import source_fixture


class DataQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.data = self.root / 'data'
        self.target = self.root / 'corrected'
        self.issues = self.root / 'approved.csv'
        self.reference = self.root / 'reference.json'
        source_fixture(self.source)
        prepare_exploratory(self.source, self.data)
        for prefix in ('execution', 'signals'):
            path = self.data / f'{prefix}_7203.T.csv'
            frame = pd.read_csv(path)
            frame.loc[0, 'High'] = 99.999999999999
            frame.loc[1, 'Low'] = 101.
            frame.to_csv(path, index=False)
        pd.DataFrame({'ticker': ['7203.T', '7203.T'],
                      'Date': frame.Date.iloc[:2].tolist()}).to_csv(self.issues, index=False)
        self.refresh_reference()

    def refresh_reference(self):
        *_, hashes = load_dataset(self.data, mode='exploratory')
        self.reference.write_text(json.dumps({'input_hashes': hashes}), encoding='utf-8')

    def correct(self):
        return correct_exploratory_data(self.data, self.target, self.issues, self.reference)

    def test_envelope_roundtrip_and_original_hashes_and_strict_gate(self):
        files = [*self.data.glob('*'), *self.source.glob('*')]
        original = {path: digest(path) for path in files}
        _, before, _, _, _, _ = load_dataset(self.data, mode='exploratory')
        status = self.correct()
        self.assertEqual(status['corrected_rows'], 2)
        self.assertEqual(status['rounding_scale_rows'], 1)
        self.assertEqual(status['substantive_rows'], 1)
        self.assertEqual(status['status'], 'completed')
        _, after, signals, _, _, _ = load_dataset(self.target, mode='exploratory')
        frame = after['7203.T']
        self.assertEqual(frame.High.iloc[0], 100.)
        self.assertEqual(frame.Low.iloc[1], 100.)
        for ticker in before:
            pd.testing.assert_frame_equal(before[ticker][['Open', 'Close', 'Volume']],
                                          after[ticker][['Open', 'Close', 'Volume']], check_exact=True)
            pd.testing.assert_frame_equal(after[ticker][['Open', 'High', 'Low', 'Close', 'Volume']],
                                          signals[ticker], check_exact=True)
        self.assertEqual(original, {path: digest(path) for path in files})
        with self.assertRaisesRegex(ValueError, 'nominal prices'):
            load_dataset(self.target)
        with self.assertRaisesRegex(ValueError, 'new output'):
            self.correct()

    def test_changed_input_hash_fails_before_writing(self):
        with (self.data / 'execution_7203.T.csv').open('a') as output:
            output.write('\n')
        with self.assertRaisesRegex(ValueError, 'Input hashes'):
            self.correct()
        self.assertFalse(self.target.exists())

    def test_extra_invalid_row_is_not_silently_corrected(self):
        for prefix in ('execution', 'signals'):
            path = self.data / f'{prefix}_7203.T.csv'
            frame = pd.read_csv(path)
            frame.loc[2, 'High'] = 98.
            frame.to_csv(path, index=False)
        self.refresh_reference()
        with self.assertRaisesRegex(ValueError, 'Unapproved OHLC issue'):
            self.correct()
        self.assertFalse(self.target.exists())

    def test_duplicate_or_missing_approved_rows_fail(self):
        issues = pd.read_csv(self.issues)
        pd.concat([issues, issues.iloc[:1]]).to_csv(self.issues, index=False)
        with self.assertRaisesRegex(ValueError, 'unique'):
            self.correct()
        issues.loc[len(issues)] = ['7203.T', '2020-12-10']
        issues.to_csv(self.issues, index=False)
        with self.assertRaisesRegex(ValueError, 'missing or no-longer-invalid'):
            self.correct()

    def test_nonfinite_and_nonpositive_values_fail(self):
        for value, message in [(float('nan'), 'Nonfinite'), (0., 'Nonpositive')]:
            with self.subTest(value=value):
                for prefix in ('execution', 'signals'):
                    path = self.data / f'{prefix}_7203.T.csv'
                    frame = pd.read_csv(path)
                    frame.loc[3, 'Open'] = value
                    frame.to_csv(path, index=False)
                self.refresh_reference()
                with self.assertRaisesRegex(ValueError, message):
                    self.correct()
                self.assertFalse(self.target.exists())
