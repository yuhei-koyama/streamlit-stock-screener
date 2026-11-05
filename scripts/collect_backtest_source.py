#!/usr/bin/env python3
"""Cache authorized daily histories once; --resume verifies and reuses saved files.

Stop at the first provider failure instead of repeatedly hitting a denied/rate-limited API.
"""
import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import pandas as pd
import yfinance as yf
from stock_screener.exploratory_data import digest
from stock_screener.universe import STOCK_TICKERS, ETF_TICKERS


def collect(output, resume=False, pause=.5):
    output=Path(output)
    tickers=STOCK_TICKERS+ETF_TICKERS
    path=output/'collection.json'
    if resume:
        metadata=json.loads(path.read_text(encoding='utf-8'))
        if (metadata.get('provider') != 'yfinance/Yahoo Finance'
            or metadata.get('start') != '2019-01-01'
            or metadata.get('end_exclusive') != '2026-01-01'
            or metadata.get('kind') != 'market'):
            raise ValueError('Cache provenance does not match this fixed collection')
        assets=metadata.get('assets',[])
        if len(assets)!=len(set(assets)) or not set(assets).issubset(tickers):
            raise ValueError('Invalid cached universe')
        for ticker in assets:
            for prefix in ('source','signals'):
                cached=output/f'{prefix}_{ticker}.csv'
                expected=metadata.get('hashes',{}).get(cached.name)
                if not expected or not cached.exists() or digest(cached)!=expected:
                    raise ValueError(f'Cached file changed or missing: {cached.name}')
    else:
        output.mkdir(parents=True,exist_ok=False)
        metadata=dict(status='collecting',kind='market',provider='yfinance/Yahoo Finance',
                      start='2019-01-01',end_exclusive='2026-01-01',assets=[],hashes={},
                      retrieved_at=pd.Timestamp.now(tz='UTC').isoformat(),
                      warning='Source OHLC is not asserted to be historical nominal prices. Use explicit exploratory mode or verify lots/actions.')
    metadata['status']='collecting'
    metadata.pop('error',None)

    def checkpoint():
        path.write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')

    checkpoint()
    try:
        for ticker in tickers:
            if ticker in metadata['assets']:
                print(f'Cached: {ticker}',flush=True)
                continue
            cached=output/f'source_{ticker}.csv'
            adjusted=output/f'signals_{ticker}.csv'
            if cached.exists() or adjusted.exists():
                raise ValueError(f'Unrecorded cache for {ticker}: inspect it before retrying; files were preserved')
            print(f'Fetching {ticker} ({len(metadata["assets"])+1}/{len(tickers)})',flush=True)
            frame=yf.Ticker(ticker).history(start='2019-01-01',end='2026-01-01',interval='1d',
                                           auto_adjust=False,actions=True,raise_errors=True)
            if frame.empty:
                raise ValueError(f'No data for {ticker}')
            # This is the same yfinance price adjustment used by history(auto_adjust=True).
            signals=yf.utils.auto_adjust(frame)
            frame.to_csv(cached,index_label='Date')
            signals.to_csv(adjusted,index_label='Date')
            for saved in (cached,adjusted):
                metadata['hashes'][saved.name]=digest(saved)
            metadata['assets'].append(ticker)
            metadata.setdefault('asset_retrieved_at',{})[ticker]=pd.Timestamp.now(tz='UTC').isoformat()
            checkpoint()
            if len(metadata['assets']) < len(tickers):
                time.sleep(pause)
        metadata['status']='source_collected_requires_execution_validation'
    except Exception as exc:
        metadata.update(status='blocked',error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        checkpoint()
    return metadata


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    parser.add_argument('--resume',action='store_true',help='Reuse a hash-verified partial/completed cache')
    args=parser.parse_args()
    collect(args.output,resume=args.resume)
