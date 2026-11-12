#!/usr/bin/env python3
"""取得不能銘柄の最大期間応答を保存し、評価期間の履歴の有無を再確認する。"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
import time

import numpy as np
import pandas as pd
from curl_cffi import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock_screener.market_research import frozen_assets, write_json


def diagnose(root, output, pause=1.):
    root, output = Path(root), Path(output)
    universe, _ = frozen_assets(root)
    collection_content = (root / 'collection.json').read_bytes()
    collection = json.loads(collection_content)
    if collection['universe_hash'] != universe['universe_hash']:
        raise ValueError('取得一覧と母集団が一致しません')
    tickers = sorted(t for t, item in collection['assets'].items()
                     if item['status'] in ('blocked_data', 'unavailable'))
    expected = dict(universe_hash=universe['universe_hash'],
                    collection_sha256=sha256(collection_content).hexdigest(),
                    code_sha256=sha256(Path(__file__).read_bytes()).hexdigest())
    state_path = output / 'run.json'
    if output.exists():
        state = json.loads(state_path.read_text(encoding='utf-8'))
        if any(state.get(key) != value for key, value in expected.items()):
            raise ValueError('診断の入力またはコードが変わっています。新規作業先が必要です')
        for ticker, record in state['assets'].items():
            if record['status'] == 'checked' and sha256((output / (ticker + '.json')).read_bytes()).hexdigest() != record['response_sha256']:
                raise ValueError('保存済み診断原本が変わっています')
    else:
        output.mkdir(parents=True)
        (output / 'collection_snapshot.json').write_bytes(collection_content)
        state = dict(status='running', **expected, assets={})
        write_json(state_path, state)
    with requests.Session(impersonate='chrome') as session:
        for ticker in tickers:
            if state['assets'].get(ticker, {}).get('status') == 'checked':
                continue
            try:
                response = session.get(f'https://query2.finance.yahoo.com/v8/finance/chart/{ticker}',
                                       params=dict(range='max', interval='1d', events='div,splits'), timeout=30)
                content = response.content
                (output / (ticker + '.json')).write_bytes(content)
                if response.status_code not in (200, 400, 404):
                    response.raise_for_status()
                payload = response.json()
                result = payload.get('chart', {}).get('result')
                record = dict(status='checked', http_status=response.status_code,
                              response_sha256=sha256(content).hexdigest(), historical_valid_close_rows=0,
                              error=payload.get('chart', {}).get('error'))
                if result:
                    data = result[0]
                    timestamps = data.get('timestamp', [])
                    quotes = data.get('indicators', {}).get('quote', [{}])[0]
                    closes = quotes.get('close', [])
                    if len(timestamps) != len(closes):
                        raise ValueError('最大期間応答の日時と価格の件数が一致しません')
                    dates = pd.to_datetime(timestamps, unit='s', utc=True).tz_convert('Asia/Tokyo')
                    values = pd.to_numeric(pd.Series(closes), errors='coerce').to_numpy(dtype=float)
                    valid = np.isfinite(values) & (values > 0)
                    historical = valid & (dates >= pd.Timestamp('2014-01-01', tz='Asia/Tokyo'))
                    historical &= dates < pd.Timestamp('2026-01-01', tz='Asia/Tokyo')
                    valid_dates = dates[valid]
                    record.update(symbol=data.get('meta', {}).get('symbol'),
                                  granularity=data.get('meta', {}).get('dataGranularity'),
                                  provider_first_trade_epoch=data.get('meta', {}).get('firstTradeDate'),
                                  first_observed_date=valid_dates.min().date().isoformat() if len(valid_dates) else None,
                                  last_observed_date=valid_dates.max().date().isoformat() if len(valid_dates) else None,
                                  historical_valid_close_rows=int(historical.sum()),
                                  duplicate_session_dates=int(dates.normalize().duplicated().sum()))
                state['assets'][ticker] = record
                write_json(state_path, state)
                print(f'確認 {ticker} 過去期間の有効終値{record["historical_valid_close_rows"]}行', flush=True)
                time.sleep(pause)
            except Exception as exc:
                state['assets'][ticker] = dict(status='error', reason=f'{type(exc).__name__}: {exc}')
                state['status'] = 'interrupted'
                write_json(state_path, state)
                raise
    frame = pd.DataFrame([dict(ticker=ticker, **record) for ticker, record in state['assets'].items()])
    frame.to_csv(output / 'summary.csv', index=False, encoding='utf-8-sig')
    state.update(status='completed', checked_tickers=len(frame),
                 historical_data_returned_tickers=int(frame.historical_valid_close_rows.gt(0).sum()) if len(frame) else 0)
    write_json(state_path, state)
    (output / 'REPORT.md').write_text('# 取得不能銘柄の追加確認\n\n最大期間指定のYahoo応答を原本として保存した。summary.csvに日時範囲と2014〜2025年の有効終値件数を記録する。最大期間応答は診断専用で、通常の価格キャッシュの補正・除外・置換は行っていない。全件の扱いは別途判断する。\n', encoding='utf-8')
    return state


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--pause', type=float, default=1.)
    args = parser.parse_args()
    diagnose(args.root, args.output, args.pause)
