"""Explicit verified-data contract and repeatable fixed-suite artifacts."""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import subprocess

import pandas as pd

from .backtest import Config, run_backtest
from .backtest_signals import STRATEGIES, screening_signals
from .universe import STOCK_TICKERS, ETF_TICKERS
from .exploratory_data import BASIS, PRICE_COLUMNS, WARNINGS


def load_dataset(directory, require_approved_universe=True, mode="strict"):
    root=Path(directory)
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    if mode == 'strict':
        if manifest.get('execution_price_basis') != 'nominal_unadjusted':
            raise ValueError('Execution OHLC must be verified historical nominal prices, not split/dividend-adjusted')
        if manifest.get('actions_verified') is not True or manifest.get('lot_sizes_verified') is not True:
            raise ValueError('Corporate actions/payment dates and date-specific lot sizes must be verified')
    elif mode == 'exploratory':
        if (manifest.get('execution_price_basis') != BASIS
            or manifest.get('dividend_mode') != 'embedded_in_adjusted_prices'
            or manifest.get('lot_size_model') != 'virtual_one_share'):
            raise ValueError('Exploratory mode requires explicit adjusted-price / virtual-one-share metadata')
    else:
        raise ValueError('mode must be strict or exploratory')
    if manifest.get('kind') not in ('market','synthetic'):
        raise ValueError('kind must explicitly be market or synthetic')
    if not manifest.get('provider') or not manifest.get('retrieved_at'):
        raise ValueError('provider/retrieved_at provenance required')
    entries=manifest.get('assets',[])
    tickers=[item['ticker'] for item in entries]
    if len(tickers) != len(set(tickers)) or not tickers:
        raise ValueError('duplicate or empty universe')
    if require_approved_universe and set(tickers) != set(STOCK_TICKERS+ETF_TICKERS):
        raise ValueError('Approved run requires exactly the fixed 70 tickers; partial universes cannot be silently ranked')
    expected={**dict.fromkeys(STOCK_TICKERS,'stock'),**dict.fromkeys(ETF_TICKERS,'etf')}
    execution, inputs, classes, hashes = {}, {}, {}, {}
    for asset in entries:
        ticker=asset['ticker']
        if ticker not in expected or asset['asset_class'] != expected[ticker]:
            raise ValueError(f'Unexpected asset/class: {ticker}')
        for prefix,destination in [('execution',execution),('signals',inputs)]:
            path=root/f'{prefix}_{ticker}.csv'
            hashes[path.name]=sha256(path.read_bytes()).hexdigest()
            frame=pd.read_csv(path,index_col='Date',parse_dates=['Date'])
            # Normalize session labels consistently without filling any missing rows.
            if frame.index.tz is not None:
                frame.index=frame.index.tz_convert('Asia/Tokyo').tz_localize(None)
            frame.index=frame.index.normalize()
            destination[ticker]=frame
        if mode == 'exploratory':
            virtual=execution[ticker]
            required=set(PRICE_COLUMNS+['LotSize','Split','Dividend','DividendPayDate'])
            if not required.issubset(virtual.columns):
                raise ValueError('Incomplete virtual-share execution input')
            if (not virtual.LotSize.eq(1).all() or not virtual.Split.eq(1).all()
                or not virtual.Dividend.eq(0).all() or not virtual.DividendPayDate.isna().all()):
                raise ValueError('Exploration requires one-share lots and no separate split/dividend accounting')
            if not virtual[PRICE_COLUMNS].equals(inputs[ticker][PRICE_COLUMNS]):
                raise ValueError('Exploratory execution and screener OHLCV must be identical')
        classes[ticker]=asset['asset_class']
    path=root/'calendar.csv'
    hashes[path.name]=sha256(path.read_bytes()).hexdigest()
    calendar=pd.DatetimeIndex(pd.read_csv(path,parse_dates=['Date']).Date)
    hashes['manifest.json']=sha256((root/'manifest.json').read_bytes()).hexdigest()
    return manifest,execution,inputs,classes,calendar,hashes


def run_suite(directory, output, config=None, mode="strict", progress=None):
    manifest,execution,inputs,classes,calendar,hashes=load_dataset(directory, mode=mode)
    cfg=config or Config()
    if '1306.T' not in inputs:
        raise ValueError('1306.T RS benchmark required')
    output=Path(output)
    if output.exists():
        raise ValueError('Use a new output directory; do not overwrite previous runs')
    output.mkdir(parents=True)
    try:
        revision=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
        dirty=bool(subprocess.check_output(['git','status','--porcelain'],text=True).strip())
    except subprocess.CalledProcessError:
        revision,dirty='unknown',True
    code_hashes={p.name:sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')}
    status=dict(status='running',kind=manifest['kind'],execution_mode=mode,
                execution_price_basis=manifest['execution_price_basis'],code_commit=revision,code_dirty=dirty,code_hashes=code_hashes,
                settings=asdict(cfg),input_hashes=hashes,provider=manifest['provider'],
                retrieved_at=manifest['retrieved_at'],started_at=pd.Timestamp.now(tz='UTC').isoformat(),
                warnings=['Exploratory; current 70-ticker universe has survivorship/selection bias.',
                          'Limit-locks and liquidity are modeled only if CanBuy/CanSell are supplied.',
                          'No parameter optimization or out-of-sample profitability claim.',
                          'No authentic All Country benchmark supplied; no outperformance claim.'])
    if mode == 'exploratory':
        status['warnings'].extend(WARNINGS)
    status_path=output/'run.json'
    status_path.write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')
    summary=[]
    try:
        for strategy in STRATEGIES:
            if progress:
                progress(f'Computing screener signals: {strategy}')
            # Exactly one call to the screener function for each asset/strategy.
            flags={t:screening_signals(f,strategy,inputs['1306.T'].Close) for t,f in inputs.items()}
            for policy in ('A','B'):
                if progress:
                    progress(f'Running {len(summary)+1}/14: {strategy} / exit {policy}')
                result=run_backtest(execution,flags,strategy,policy,calendar,classes,cfg)
                result.config.update(execution_mode=mode, execution_price_basis=manifest['execution_price_basis'])
                name=f'{strategy}__{policy}'
                target=output/name
                target.mkdir()
                for field in ('daily','trades','rejections','positions','asset_groups'):
                    getattr(result,field).to_csv(target/f'{field}.csv',index=field=='daily')
                result.annual_returns.to_csv(target/'annual_returns.csv')
                result.monthly_returns.to_csv(target/'monthly_returns.csv')
                (target/'metrics.json').write_text(json.dumps(result.metrics,indent=2,allow_nan=False),encoding='utf-8')
                (target/'config.json').write_text(json.dumps(result.config,indent=2),encoding='utf-8')
                summary.append(dict(strategy_id=strategy,exit_policy=policy,**result.metrics,
                                    rejected_orders=len(result.rejections)))
                status['completed_conditions']=len(summary)
                status_path.write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')
        pd.DataFrame(summary).to_csv(output/'summary.csv',index=False)
        status.update(status='completed',completed_conditions=len(summary),
                      completed_at=pd.Timestamp.now(tz='UTC').isoformat())
    except Exception as exc:
        status.update(status='failed',failure=f'{type(exc).__name__}: {exc}',completed_conditions=len(summary))
        raise
    finally:
        status_path.write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')
    return pd.DataFrame(summary)


def package_results(directory):
    """Put run.json at ZIP root; never package a partial run or overwrite a ZIP."""
    import shutil
    directory=Path(directory)
    status=json.loads((directory/'run.json').read_text(encoding='utf-8'))
    if status.get('status')!='completed' or status.get('completed_conditions')!=14:
        raise ValueError('Only completed 14-condition runs can be packaged')
    output=Path(str(directory)+'.zip')
    if output.exists():
        raise ValueError('Result ZIP already exists')
    return shutil.make_archive(str(directory),'zip',root_dir=directory)
