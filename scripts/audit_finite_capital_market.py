#!/usr/bin/env python3
"""固定母集団の全取得結果を補正せず監査し、判断が必要な一覧を保存する。"""
import argparse
from hashlib import sha256
import io
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from stock_screener.exploratory_data import PRICE_COLUMNS
from stock_screener.market_research import frozen_assets
from stock_screener.trading_calendar import split_trading_rows, trading_day_mask, calendar_metadata

APPROVALS = {
    'approval-1326-20220517': '0277c39be4ca1dbb34ce9895fb1a3634b54c1c17a58e694c2940dd35a41d628b',
    'approval-rounding-first25-159': '927dacb565a569ef5e69f5f5706194c0a110aa95d8fdddf5474fb6972ca21257',
}
CORRECTION_COLUMNS = ['ticker', 'Date', 'Open', 'Close', 'original_high', 'original_low',
                      'proposed_high', 'proposed_low', 'difference', 'relative_difference', 'source_sha256']


def approved_rows():
    rows = {}
    base = ROOT / 'artifacts/finite-capital-universe-20261107-01'
    for folder, expected_hash in APPROVALS.items():
        record_content = (base / folder / 'approval.json').read_bytes()
        if sha256(record_content).hexdigest() != expected_hash:
            raise ValueError('固定承認レコードが一致しません')
        record = json.loads(record_content)
        content = (base / folder / 'approved_corrections.csv').read_bytes()
        if sha256(content).hexdigest() != record['corrections_sha256']:
            raise ValueError('固定承認一覧が一致しません')
        frame = pd.read_csv(io.BytesIO(content), float_precision='round_trip')
        if len(frame) != record['correction_rows'] or frame.duplicated(['ticker', 'Date']).any():
            raise ValueError('固定承認の件数・重複が不正です')
        for row in frame.to_dict('records'):
            key = (row['ticker'], row['Date'])
            if key in rows:
                raise ValueError('固定承認が重複しています')
            rows[key] = row
    return rows


def audit(root, output):
    root, output = Path(root), Path(output)
    universe, assets = frozen_assets(root)
    collection_content = (root / 'collection.json').read_bytes()
    collection = json.loads(collection_content)
    if collection['universe_hash'] != universe['universe_hash']:
        raise ValueError('価格取得と母集団が一致しません')
    approved = approved_rows()
    rows, invalid, coverage, exclusions, approved_found, missing_internal = [], [], [], [], [], []
    expected = pd.date_range('2014-01-01', '2025-12-31')
    expected = expected[trading_day_mask(expected)]
    observed = set()
    reference_dates = pd.DatetimeIndex([])
    for asset in assets.itertuples():
        ticker = asset.ticker
        saved = collection['assets'].get(ticker, {'status': 'not_collected'})
        for name, value in saved.get('hashes', {}).items():
            path = (root / 'cache' / name).resolve()
            if not path.is_relative_to((root / 'cache').resolve()) or sha256(path.read_bytes()).hexdigest() != value:
                raise ValueError(f'取得原本のハッシュが変わっています: {ticker}')
        item = dict(ticker=ticker, asset_class=asset.asset_class, status=saved['status'],
                    reason=saved.get('reason', ''), valid_rows=0, invalid_rows=0, proposed_rows=0)
        if saved['status'] != 'ready':
            coverage.append(item)
            continue
        name = ticker + '.csv'
        content = (root / 'cache' / name).read_bytes()
        source_hash = sha256(content).hexdigest()
        if source_hash != saved['hashes'][name]:
            raise ValueError('監査中に価格入力が変わりました')
        frame = pd.read_csv(io.BytesIO(content), index_col='Date', parse_dates=['Date'], float_precision='round_trip')
        if not frame.index.is_unique or not frame.index.is_monotonic_increasing:
            item.update(status='blocked_data', reason='日付が重複または未整列')
            coverage.append(item)
            continue
        frame, off = split_trading_rows(frame)
        exclusions.append(off.reset_index().assign(ticker=ticker))
        observed.update(frame.index)
        if ticker == '1306.T':
            reference_dates = frame.index
        bad = (~np.isfinite(frame[PRICE_COLUMNS]).all(axis=1))
        bad |= frame[['Open', 'High', 'Low', 'Close']].le(0).any(axis=1) | frame.Volume.lt(0)
        for date, row in frame[bad].iterrows():
            invalid.append(dict(ticker=ticker, Date=date.date().isoformat(),
                                **row[PRICE_COLUMNS].to_dict(), source_sha256=source_hash))
        edges = frame.High.lt(frame[['Open', 'Close', 'Low']].max(axis=1))
        edges |= frame.Low.gt(frame[['Open', 'Close', 'High']].min(axis=1))
        proposed_count = 0
        for date, row in frame[edges & ~bad].iterrows():
            high = max(row.Open, row.Close, row.High, row.Low)
            low = min(row.Open, row.Close, row.High, row.Low)
            difference = max(high - row.High, row.Low - low)
            proposal = dict(ticker=ticker, Date=date.date().isoformat(), Open=row.Open, Close=row.Close,
                            original_high=row.High, original_low=row.Low, proposed_high=high,
                            proposed_low=low, difference=difference,
                            relative_difference=difference / max(abs(row.Open), abs(row.Close)),
                            source_sha256=source_hash)
            known = approved.get((ticker, proposal['Date']))
            if known is not None and all(known[name] == proposal[name] for name in CORRECTION_COLUMNS):
                approved_found.append(proposal)
            else:
                rows.append(proposal)
                proposed_count += 1
        if len(frame):
            missing = expected[(expected >= frame.index[0]) & (expected <= frame.index[-1])].difference(frame.index)
            missing_internal.extend(dict(ticker=ticker, Date=d.date().isoformat()) for d in missing)
            item.update(first_date=frame.index[0].date().isoformat(), last_date=frame.index[-1].date().isoformat(),
                        zero_volume_rows=int(frame.Volume.eq(0).sum()), internal_missing_sessions=len(missing))
        item.update(valid_rows=int((~bad).sum()), invalid_rows=int(bad.sum()), proposed_rows=proposed_count,
                    excluded_nontrading_rows=len(off))
        coverage.append(item)
    proposals = pd.DataFrame(rows, columns=CORRECTION_COLUMNS)
    coverage = pd.DataFrame(coverage)
    missing_all = expected.difference(pd.DatetimeIndex(sorted(observed)))
    missing_rs = expected.difference(reference_dates)
    summary = dict(total_universe=universe['asset_count'], collected_ready=int(coverage.status.eq('ready').sum()),
                   status_counts=coverage.status.value_counts().to_dict(), invalid_price_rows=len(invalid),
                   unapproved_correction_rows=len(rows),
                   unapproved_rounding_rows=int(proposals.difference.lt(1e-8).sum()),
                   unapproved_substantive_rows=int(proposals.difference.ge(1e-8).sum()),
                   previously_approved_matching_rows=len(approved_found),
                   excluded_nontrading_rows=sum(len(f) for f in exclusions),
                   missing_all_market_dates=missing_all.strftime('%Y-%m-%d').tolist(),
                   missing_reference_dates=missing_rs.strftime('%Y-%m-%d').tolist(),
                   internal_missing_ticker_sessions=len(missing_internal),
                   collection_complete=not coverage.status.isin(['not_collected', 'error']).any(),
                   corrections_applied=False, universe_hash=universe['universe_hash'],
                   collection_sha256=sha256(collection_content).hexdigest(), trading_calendar=calendar_metadata(),
                   code_hashes={str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest()
                                for p in [Path(__file__).resolve(), ROOT / 'stock_screener/market_research.py',
                                          ROOT / 'stock_screener/trading_calendar.py']})
    if output.exists():
        raise ValueError('新規の監査出力先を指定してください')
    output.mkdir(parents=True)
    coverage.to_csv(output / 'coverage.csv', index=False, encoding='utf-8-sig')
    proposals.to_csv(output / 'proposed_corrections.csv', index=False, encoding='utf-8-sig', float_format='%.17g')
    pd.DataFrame(approved_found, columns=CORRECTION_COLUMNS).to_csv(output / 'previously_approved.csv', index=False, float_format='%.17g')
    pd.DataFrame(invalid, columns=['ticker', 'Date', *PRICE_COLUMNS, 'source_sha256']).to_csv(output / 'invalid_prices.csv', index=False, float_format='%.17g')
    coverage[~coverage.status.eq('ready')].to_csv(output / 'collection_issues.csv', index=False, encoding='utf-8-sig')
    pd.DataFrame(missing_internal, columns=['ticker', 'Date']).to_csv(output / 'internal_missing_sessions.csv', index=False)
    if exclusions:
        pd.concat(exclusions, ignore_index=True).to_csv(output / 'excluded_nontrading_rows.csv', index=False, float_format='%.17g')
    (output / 'collection_snapshot.json').write_bytes(collection_content)
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=bool), encoding='utf-8')
    report = '# 全市場価格の補正前監査\n\n'
    report += f"固定{summary['total_universe']}銘柄、価格取得済み{summary['collected_ready']}銘柄を監査した。\n\n"
    report += f"未承認の高安不整合は{len(rows)}行、非正価格・非有限値・負出来高は{len(invalid)}行。既承認160行との一致は{len(approved_found)}行。補正は未適用。\n\n"
    report += 'coverage.csvに全対象の取得状態、collection_issues.csvに取得不能・要確認、proposed_corrections.csvに補正前後の案と元CSVのハッシュを保存した。個別銘柄の日付欠損は上場前後と断定せず、観測範囲と内部欠損を区分した。主検証は未実行。\n'
    (output / 'REPORT.md').write_text(report, encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, default=bool))
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    audit(args.root, args.output)
