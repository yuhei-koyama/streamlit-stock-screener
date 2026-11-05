#!/usr/bin/env python3
"""Cache each authorized ticker once. This does NOT assert nominal execution prices.

Stops on the first provider failure instead of hammering a denied/rate-limited API.
"""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import pandas as pd
import yfinance as yf
from stock_screener.universe import STOCK_TICKERS, ETF_TICKERS


def collect(output):
    output=Path(output)
    output.mkdir(parents=True,exist_ok=False)
    metadata=dict(status='collecting',provider='yfinance/Yahoo Finance',
                  start='2019-01-01',end_exclusive='2026-01-01',
                  retrieved_at=pd.Timestamp.now(tz='UTC').isoformat(),assets=[],
                  warning='Source OHLC is not asserted to be historical nominal prices. Verify lots/corporate actions before execution.')
    path=output/'collection.json'
    try:
        for ticker in STOCK_TICKERS+ETF_TICKERS:
            cached=output/f'source_{ticker}.csv'
            if cached.exists():
                raise ValueError(f'Existing cache {cached.name}: inspect provenance rather than overwrite or refetch')
            frame=yf.Ticker(ticker).history(start='2019-01-01',end='2026-01-01',interval='1d',
                                           auto_adjust=False,actions=True,raise_errors=True)
            if frame.empty:
                raise ValueError(f'No data for {ticker}')
            frame.to_csv(cached,index_label='Date')
            yf.utils.auto_adjust(frame).to_csv(output/f'signals_{ticker}.csv',index_label='Date')
            metadata['assets'].append(ticker)
        metadata['status']='source_collected_requires_execution_validation'
    except Exception as exc:
        metadata.update(status='blocked',error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        path.write_text(json.dumps(metadata,ensure_ascii=False,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    collect(args.output)
