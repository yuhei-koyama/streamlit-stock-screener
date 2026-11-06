#!/usr/bin/env python3
"""許可済みYahoo chartから2014〜2020年の日足を保存する。"""
import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from curl_cffi import requests
import pandas as pd
import yfinance as yf
from stock_screener.exploratory_data import digest
from stock_screener.universe import STOCK_TICKERS, ETF_TICKERS


def collect(output):
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    status_path=output/'collection.json'
    status=json.loads(status_path.read_text(encoding='utf-8')) if status_path.exists() else dict(
        kind='market',status='collecting',provider='Yahoo Finance query2 chart / yfinance parse_quotes+auto_adjust',
        retrieved_at=pd.Timestamp.now(tz='UTC').isoformat(),start='2014-01-01',end_exclusive='2021-01-01',
        assets=[],hashes={})
    tickers=STOCK_TICKERS+ETF_TICKERS
    with requests.Session(impersonate='chrome') as session:
        try:
            for ticker in tickers:
                if ticker in status['assets']:
                    for prefix,extension in [('signals','csv'),('source','csv'),('response','json')]:
                        p=output/f'{prefix}_{ticker}.{extension}'
                        if digest(p)!=status['hashes'][p.name]:
                            raise ValueError(f'Cache changed: {p.name}')
                    continue
                p=output/f'response_{ticker}.json'
                if not p.exists():
                    response=session.get(f'https://query2.finance.yahoo.com/v8/finance/chart/{ticker}',
                        params=dict(period1=1388534400,period2=1609459200,interval='1d',events='div,splits'),timeout=30)
                    response.raise_for_status()
                    p.write_text(response.text,encoding='utf-8')
                data=json.loads(p.read_text(encoding='utf-8'))
                if data['chart'].get('error'):
                    raise ValueError(str(data['chart']['error']))
                result=data['chart']['result'][0]
                quotes=yf.utils.parse_quotes(result)
                if quotes.empty:
                    raise ValueError(f'No history: {ticker}')
                quotes.index=quotes.index.tz_localize('UTC').tz_convert('Asia/Tokyo').tz_localize(None).normalize()
                quotes=quotes.loc['2014-01-01':'2020-12-31']
                for prefix,frame in [('source',quotes),('signals',yf.utils.auto_adjust(quotes))]:
                    f=output/f'{prefix}_{ticker}.csv'
                    frame.to_csv(f,index_label='Date',float_format='%.17g')
                    status['hashes'][f.name]=digest(f)
                status['hashes'][p.name]=digest(p)
                status['assets'].append(ticker)
                print(f'{len(status["assets"])}/{len(tickers)} {ticker}: {len(quotes)} rows {quotes.index[0].date()}..{quotes.index[-1].date()}',flush=True)
                status_path.write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')
                time.sleep(.3)
            status['status']='source_collected_requires_execution_validation'
            status.pop('error',None)
        except Exception as exc:
            status.update(status='blocked',error=f'{type(exc).__name__}: {exc}')
            raise
        finally:
            status_path.write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    collect(args.output)
