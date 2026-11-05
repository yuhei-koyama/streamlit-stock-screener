"""Build explicitly virtual-share execution inputs from cached screener prices."""
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .universe import STOCK_TICKERS, ETF_TICKERS

PRICE_COLUMNS = ['Open','High','Low','Close','Volume']
BASIS = 'adjusted_virtual_shares'
WARNINGS = [
    'Adjusted OHLC and virtual integer shares, not historical nominal prices or actual trading lots.',
    'Splits/dividends are embedded in adjusted prices; no separate dividend cash or split share changes.',
    'Stops and position sizes are simulated on the adjusted price scale.',
    'Calendar is the union of observed Yahoo sessions; market-wide missing sessions are not detectable.',
]


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def prepare_exploratory(source, output):
    """Offline conversion; do not infer real lots or fake verified corporate actions."""
    source,output=Path(source),Path(output)
    metadata=json.loads((source/'collection.json').read_text(encoding='utf-8'))
    if metadata.get('status') != 'source_collected_requires_execution_validation':
        raise ValueError('Source collection is incomplete; resume collection first')
    tickers=STOCK_TICKERS+ETF_TICKERS
    if len(metadata.get('assets',[])) != len(tickers) or set(metadata['assets']) != set(tickers):
        raise ValueError('Exactly the approved 70 tickers are required')
    if metadata.get('kind') not in ('market','synthetic'):
        raise ValueError('Source must declare market or synthetic kind')
    if not metadata.get('provider') or not metadata.get('retrieved_at'):
        raise ValueError('Source provenance is missing')
    if output.exists():
        raise ValueError('Use a new output directory; do not overwrite a dataset')
    frames={}
    source_hashes={}
    for ticker in tickers:
        path=source/f'signals_{ticker}.csv'
        expected=metadata.get('hashes',{}).get(path.name)
        if not expected or digest(path) != expected:
            raise ValueError(f'Source cache hash mismatch or missing hash: {path.name}')
        source_hashes[path.name]=expected
        frame=pd.read_csv(path,index_col='Date',parse_dates=['Date'])
        if not set(PRICE_COLUMNS).issubset(frame.columns) or frame.empty:
            raise ValueError(f'Empty or incomplete OHLCV: {ticker}')
        if frame.index.tz is not None:
            frame.index=frame.index.tz_convert('Asia/Tokyo').tz_localize(None)
        frame.index=frame.index.normalize()
        if not frame.index.is_unique or not frame.index.is_monotonic_increasing:
            raise ValueError(f'Unsorted/duplicate dates: {ticker}')
        prices=frame[PRICE_COLUMNS].copy()
        if not all(pd.api.types.is_numeric_dtype(prices[c]) for c in PRICE_COLUMNS):
            raise ValueError(f'OHLCV must be numeric: {ticker}')
        if np.isinf(prices.to_numpy()).any():
            raise ValueError(f'Infinite OHLCV: {ticker}')
        frames[ticker]=prices
    output.mkdir(parents=True)
    manifest=dict(kind=metadata['kind'],provider=metadata['provider'],
                  retrieved_at=metadata['retrieved_at'],execution_price_basis=BASIS,
                  actions_verified=False,lot_sizes_verified=False,
                  dividend_mode='embedded_in_adjusted_prices',lot_size_model='virtual_one_share',
                  source_hashes=source_hashes,
                  source_collection_hash=digest(source/'collection.json'),
                  calendar_model='union_of_observed_sessions',warnings=WARNINGS,assets=[])
    calendar=pd.DatetimeIndex([])
    for ticker,prices in frames.items():
        calendar=calendar.union(prices.index)
        prices.to_csv(output/f'signals_{ticker}.csv',index_label='Date')
        execution=prices.assign(LotSize=1,Split=1.,Dividend=0.,DividendPayDate=None)
        execution.to_csv(output/f'execution_{ticker}.csv',index_label='Date')
        manifest['assets'].append(dict(ticker=ticker,asset_class='stock' if ticker in STOCK_TICKERS else 'etf'))
    pd.DataFrame({'Date':calendar.sort_values()}).to_csv(output/'calendar.csv',index=False)
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return manifest
