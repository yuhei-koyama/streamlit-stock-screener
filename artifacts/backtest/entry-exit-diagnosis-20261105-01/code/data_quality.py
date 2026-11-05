"""承認済みの行に限定して探索入力の高値・安値を整える。"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .backtest import _validate_execution
from .backtest_io import load_dataset
from .exploratory_data import PRICE_COLUMNS, digest


POLICY = 'ohlc_envelope_v1_1.0-approved'


def correct_exploratory_data(directory, output, approved_issues, reference_run):
    """元の入力ハッシュを照合し、補正済み入力と監査記録を別保存する。"""
    root, target = Path(directory), Path(output)
    issues_path, reference_path = Path(approved_issues), Path(reference_run)
    if target.exists():
        raise ValueError('Use a new output directory; do not overwrite data')
    manifest, execution, inputs, classes, calendar, hashes = load_dataset(root, mode='exploratory')
    reference = json.loads(reference_path.read_text(encoding='utf-8-sig'))
    if reference.get('input_hashes') != hashes:
        raise ValueError('Input hashes differ from the reference run')
    issues = pd.read_csv(issues_path)
    if not {'ticker', 'Date'}.issubset(issues):
        raise ValueError('Approved issue list requires ticker and Date')
    approved = {(str(row.ticker), pd.Timestamp(row.Date).normalize())
                for row in issues.itertuples()}
    if len(approved) != len(issues) or not approved:
        raise ValueError('Approved issue list must be nonempty and unique')

    audit, found = [], set()
    for ticker, frame in execution.items():
        prices = frame[PRICE_COLUMNS]
        if not np.isfinite(prices.to_numpy()).all():
            raise ValueError(f'Nonfinite OHLCV: {ticker}')
        if (prices[['Open', 'High', 'Low', 'Close']] <= 0).any().any():
            raise ValueError(f'Nonpositive OHLC: {ticker}')
        bad = ((frame.High < frame.Low)
               | (frame.High < frame[['Open', 'Close']].max(axis=1))
               | (frame.Low > frame[['Open', 'Close']].min(axis=1)))
        for date, row in frame.loc[bad].iterrows():
            key = (ticker, date)
            if key not in approved:
                raise ValueError(f'Unapproved OHLC issue: {ticker} {date.date()}')
            found.add(key)
            high = float(row[['Open', 'High', 'Low', 'Close']].max())
            low = float(row[['Open', 'High', 'Low', 'Close']].min())
            difference = max(high - float(row.High), float(row.Low) - low)
            audit.append(dict(ticker=ticker, Date=str(date.date()),
                              original_high=float(row.High), corrected_high=high,
                              original_low=float(row.Low), corrected_low=low,
                              difference=difference,
                              category='rounding_scale' if difference < 1e-8 else 'substantive'))
            frame.loc[date, ['High', 'Low']] = [high, low]
            inputs[ticker].loc[date, ['High', 'Low']] = [high, low]
        _validate_execution(frame)
    if found != approved:
        raise ValueError('Approved list contains missing or no-longer-invalid rows')

    target.mkdir(parents=True)
    status = dict(status='writing', policy=POLICY, parent_input_hashes=hashes,
                  approved_issues_hash=digest(issues_path), reference_run_hash=digest(reference_path),
                  corrected_rows=len(audit),
                  rounding_scale_rows=sum(row['category'] == 'rounding_scale' for row in audit),
                  substantive_rows=sum(row['category'] == 'substantive' for row in audit),
                  created_at=pd.Timestamp.now(tz='UTC').isoformat())
    status_path = target / 'correction.json'
    try:
        pd.DataFrame(audit).to_csv(target / 'corrections.csv', index=False)
        for ticker, frame in execution.items():
            frame.to_csv(target / f'execution_{ticker}.csv', index_label='Date')
            inputs[ticker].to_csv(target / f'signals_{ticker}.csv', index_label='Date')
        pd.DataFrame({'Date': calendar}).to_csv(target / 'calendar.csv', index=False)
        manifest.update(data_quality_policy=POLICY, csv_float_precision='round_trip',
                        corrected_rows=len(audit), parent_input_hashes=hashes,
                        rounding_scale_rows=status['rounding_scale_rows'],
                        substantive_rows=status['substantive_rows'],
                        approved_issues_hash=status['approved_issues_hash'],
                        corrections_hash=digest(target / 'corrections.csv'))
        manifest['warnings'] = [*manifest.get('warnings', []),
                                '承認された高値・安値の範囲補正を適用した探索入力です。']
        (target / 'manifest.json').write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        _, reloaded, reloaded_inputs, _, reloaded_calendar, output_hashes = load_dataset(
            target, mode='exploratory')
        for ticker in execution:
            pd.testing.assert_frame_equal(execution[ticker][PRICE_COLUMNS],
                                          reloaded[ticker][PRICE_COLUMNS], check_exact=True)
            pd.testing.assert_frame_equal(inputs[ticker][PRICE_COLUMNS],
                                          reloaded_inputs[ticker][PRICE_COLUMNS], check_exact=True)
            _validate_execution(reloaded[ticker])
        pd.testing.assert_index_equal(calendar, reloaded_calendar)
        status.update(status='completed', output_hashes=output_hashes)
    except Exception as exc:
        status.update(status='failed', failure=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
    return status
