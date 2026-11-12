"""F02。旧入力を含む各CLIの計算前検査と参照互換性を確認する。"""
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
from stock_screener.backtest_io import load_dataset, run_suite
from stock_screener.exploratory_data import digest, prepare_exploratory
from stock_screener.trading_calendar import trading_day_mask, validate_execution_calendar
from tests.test_exploratory import source_fixture


class ExecutionCalendarTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.source = self.root/'source'
        self.raw = source_fixture(self.source)
        self.data = self.root/'data'
        prepare_exploratory(self.source, self.data)

    def legacy_format(self, include_closed_rows):
        manifest_path = self.data/'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        for name in ['trading_calendar', 'exclusions_hash', 'excluded_nontrading_rows']:
            manifest.pop(name, None)
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        if include_closed_rows:
            prices = self.raw[['Open','High','Low','Close','Volume']]
            for asset in manifest['assets']:
                prices.to_csv(self.data/f"signals_{asset['ticker']}.csv", index_label='Date')
                execution = prices.assign(LotSize=1, Split=1., Dividend=0., DividendPayDate=None)
                execution.to_csv(self.data/f"execution_{asset['ticker']}.csv", index_label='Date')
            pd.DataFrame({'Date':prices.index}).to_csv(self.data/'calendar.csv', index=False)

    def test_legacy_closed_input_remains_readable_but_cannot_execute(self):
        self.legacy_format(include_closed_rows=True)
        _, _, _, _, calendar, _ = load_dataset(self.data, mode='exploratory')
        self.assertIn(pd.Timestamp('2021-01-11'), calendar)
        with self.assertRaisesRegex(ValueError, '休場日.*再準備'):
            load_dataset(self.data, mode='exploratory', for_execution=True)

    def test_both_suite_modes_stop_before_signals_and_output_creation(self):
        self.legacy_format(include_closed_rows=True)
        with patch('stock_screener.backtest_io.screening_signals') as screen:
            for evaluation in ['independent', 'portfolio']:
                output = self.root/evaluation
                with self.assertRaisesRegex(ValueError, '休場日.*再準備'):
                    run_suite(self.data, output, mode='exploratory', evaluation=evaluation)
                self.assertFalse(output.exists())
            screen.assert_not_called()

    def test_all_execution_cli_entry_points_reject_legacy_closed_input(self):
        self.legacy_format(include_closed_rows=True)
        repository = Path(__file__).resolve().parents[1]
        commands = [
            ['scripts/backtest.py', '--mode', 'exploratory', '--evaluation', 'independent'],
            ['scripts/diagnose_entry_exit.py', '--fund-csv', str(self.root/'unused-fund.csv')],
            ['scripts/validate_additional_period.py', 'run', '--previous', str(self.root/'unused-summary.csv')],
        ]
        for index, command in enumerate(commands):
            with self.subTest(command=command[0]):
                output = self.root/f'cli-output-{index}'
                result = subprocess.run(
                    [sys.executable, *command, '--data', str(self.data), '--output', str(output)],
                    cwd=repository, env={**os.environ, 'PYTHONIOENCODING':'utf-8'},
                    capture_output=True, text=True, encoding='utf-8', timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('休場日', result.stderr)
                self.assertIn('再準備', result.stderr)
                self.assertFalse(output.exists())

    def test_all_calendar_components_are_checked(self):
        closed = pd.DataFrame({'Close':[100.]}, index=pd.to_datetime(['2021-01-11']))
        opened = pd.DataFrame({'Close':[100.]}, index=pd.to_datetime(['2021-01-12']))
        cases = [
            ({'X':opened}, {'X':opened}, closed.index, None),
            ({'X':closed}, {'X':opened}, opened.index, None),
            ({'X':opened}, {'X':closed}, opened.index, None),
            ({'X':opened}, {'X':opened}, opened.index, closed),
        ]
        for execution, signals, calendar, reference in cases:
            with self.subTest(execution=execution, signals=signals, reference=reference):
                with self.assertRaisesRegex(ValueError, '休場日.*再準備'):
                    validate_execution_calendar(execution, signals, calendar, reference)

    def test_clean_legacy_input_can_run_without_forcing_a_new_format(self):
        self.legacy_format(include_closed_rows=False)
        output = self.root/'clean-run'
        summary = run_suite(self.data, output, Config(start='2021-01-04',end='2021-01-15'),
                            mode='exploratory', evaluation='independent')
        self.assertEqual(len(summary), 14)
        self.assertEqual(json.loads((output/'run.json').read_text())['status'], 'completed')

    def test_repreparation_filters_holidays_and_retains_zero_volume_open_day(self):
        path = self.source/'signals_7203.T.csv'
        prices = pd.read_csv(path, index_col='Date', parse_dates=['Date'])
        prices.loc['2021-01-12', 'Volume'] = 0
        prices.to_csv(path, index_label='Date')
        metadata_path = self.source/'collection.json'
        metadata = json.loads(metadata_path.read_text())
        metadata['hashes'][path.name] = digest(path)
        metadata_path.write_text(json.dumps(metadata))
        output = self.root/'reprepared'
        manifest = prepare_exploratory(self.source, output)
        expected = int((~trading_day_mask(prices.index)).sum())*70
        self.assertEqual(manifest['excluded_nontrading_rows'], expected)
        _, execution, signals, _, calendar, _ = load_dataset(output, mode='exploratory',for_execution=True)
        self.assertTrue(trading_day_mask(calendar).all())
        self.assertEqual(signals['7203.T'].loc['2021-01-12','Volume'], 0)
        pd.testing.assert_frame_equal(execution['7203.T'][signals['7203.T'].columns], signals['7203.T'])
        for name, expected_hash in metadata['hashes'].items():
            self.assertEqual(digest(self.source/name), expected_hash)
