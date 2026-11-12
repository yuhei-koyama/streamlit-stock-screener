#!/usr/bin/env python3
"""2016〜2020年を既存入口・診断条件のまま追加検証する。"""
import argparse
import io
from hashlib import sha256
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
from stock_screener import correction_approval
from stock_screener.backtest import Config, _validate_execution
from stock_screener.backtest_io import load_dataset
from stock_screener.backtest_signals import STRATEGIES, screening_signals
from stock_screener.entry_exit_diagnosis import HORIZONS,STOPS,fixed_horizon,entry_statistics
from stock_screener.exploratory_data import BASIS,PRICE_COLUMNS,digest
from stock_screener.independent_backtest import run_independent_backtest
from stock_screener.universe import STOCK_TICKERS,ETF_TICKERS
from stock_screener.trading_calendar import split_trading_rows, calendar_metadata, validate_execution_calendar


def save(frame,path,index=False):
    frame.to_csv(path,index=index,encoding='utf-8-sig',float_format='%.17g',compression='infer')


def prepare(source,output,approve_rounding=False):
    source,output=Path(source),Path(output)
    collection_content=(source/'collection.json').read_bytes()
    meta=json.loads(collection_content)
    if meta.get('status')!='source_collected_requires_execution_validation':
        raise ValueError('Source collection is incomplete')
    if output.exists():
        raise ValueError('Use a new data directory')
    approved = {}
    skipped_approved = []
    found = set()
    if approve_rounding:
        approval, approved_rows, approval_content, approved_list_content = correction_approval.load_rounding_approval(source, collection_content)
        approved = {(row.ticker, row.Date):row for row in approved_rows.itertuples(index=False)}
    audit=[]; frames={}; excluded_frames=[]
    for ticker in STOCK_TICKERS:
        path=source/f'signals_{ticker}.csv'
        content=path.read_bytes()
        if sha256(content).hexdigest()!=meta['hashes'][path.name]:
            raise ValueError(f'Changed source: {ticker}')
        frame=pd.read_csv(io.BytesIO(content),index_col='Date',parse_dates=['Date'],float_precision='round_trip')[PRICE_COLUMNS]
        if not frame.index.is_unique or not frame.index.is_monotonic_increasing:
            raise ValueError(f'Unsorted or duplicate source dates: {ticker}')
        for (approved_ticker, date), expected in approved.items():
            if approved_ticker != ticker:
                continue
            if date not in frame.index:
                raise ValueError(f'承認された行が元データにありません: {ticker} {date.date()}')
            before = frame.loc[date]
            actual_values = (before.Open, before.Close, before.High, before.Low)
            expected_values = (expected.Open, expected.Close, expected.original_high, expected.original_low)
            if actual_values != expected_values:
                raise ValueError(f'承認済みの補正前価格と一致しません: {ticker} {date.date()}')

        frame, excluded = split_trading_rows(frame)
        excluded_frames.append(excluded.reset_index().assign(ticker=ticker))
        for date in excluded.index:
            if (ticker, date) in approved:
                skipped_approved.append(dict(ticker=ticker,Date=date,reason='nontrading_day'))
        if not np.isfinite(frame.to_numpy()).all() or (frame[['Open','High','Low','Close']]<=0).any().any():
            raise ValueError(f'Invalid price data: {ticker}; no automatic correction')
        bad=(frame.High<frame.Low)|(frame.High<frame[['Open','Close']].max(axis=1))|(frame.Low>frame[['Open','Close']].min(axis=1))
        for date,row in frame[bad].iterrows():
            high=float(row[['Open','High','Low','Close']].max())
            low=float(row[['Open','High','Low','Close']].min())
            difference=max(high-row.High,row.Low-low)
            key = (ticker, date)
            if not approve_rounding or difference>=1e-8 or key not in approved:
                raise ValueError(f'Unapproved price issue: {ticker} {date.date()} {difference}')
            expected = approved[key]
            if (high, low, difference) != (expected.proposed_high, expected.proposed_low, expected.difference):
                raise ValueError(f'承認済みの補正後価格と一致しません: {ticker} {date.date()}')
            found.add(key)
            audit.append(dict(ticker=ticker,Date=date,original_high=row.High,corrected_high=high,
                              original_low=row.Low,corrected_low=low,difference=difference))
            frame.loc[date,['High','Low']]=[high,low]
        execution=frame.assign(LotSize=1,Split=1.,Dividend=0.,DividendPayDate=None)
        _validate_execution(execution)
        frames[ticker]=frame
    skipped_keys = {(row['ticker'], row['Date']) for row in skipped_approved}
    if approve_rounding and found != set(approved)-skipped_keys:
        raise ValueError('承認済み一覧と補正対象が一致しません')
    output.mkdir(parents=True)
    calendar=pd.DatetimeIndex([])
    for ticker,frame in frames.items():
        calendar=calendar.union(frame.index)
        save(frame,output/f'signals_{ticker}.csv',index=True)
        save(frame.assign(LotSize=1,Split=1.,Dividend=0.,DividendPayDate=None),output/f'execution_{ticker}.csv',index=True)
    save(pd.DataFrame({'Date':calendar.sort_values()}),output/'calendar.csv')
    save(pd.DataFrame(audit),output/'corrections.csv')
    save(pd.concat(excluded_frames,ignore_index=True),output/'excluded_nontrading_rows.csv')
    manifest=dict(kind=meta['kind'],provider=meta['provider'],retrieved_at=meta['retrieved_at'],
                  execution_price_basis=BASIS,dividend_mode='embedded_in_adjusted_prices',
                  lot_size_model='virtual_one_share',actions_verified=False,lot_sizes_verified=False,
                  csv_float_precision='round_trip',source_hashes=meta['hashes'],
                  source_collection_hash=sha256(collection_content).hexdigest(),
                  corrections_hash=digest(output/'corrections.csv'),rounding_rows=len(audit),
                  trading_calendar=calendar_metadata(),
                  excluded_nontrading_rows=sum(len(frame) for frame in excluded_frames),
                  exclusions_hash=digest(output/'excluded_nontrading_rows.csv'),
                  data_readiness='stock_data_validated_with_trading_calendar',
                  scope='individual_stocks_only; ETFs deferred',
                  assets=[dict(ticker=t,asset_class='stock') for t in STOCK_TICKERS])
    if approve_rounding:
        (output/'rounding_approval.json').write_bytes(approval_content)
        (output/'approved_corrections.csv').write_bytes(approved_list_content)
        save(pd.DataFrame(skipped_approved,columns=['ticker','Date','reason']),output/'skipped_approved_corrections.csv')
        manifest['rounding_approval'] = dict(
            approval_id=approval['approval_id'], record_sha256=sha256(approval_content).hexdigest(),
            approved_list_sha256=approval['approved_list_sha256'],
            source_collection_sha256=approval['source_collection_sha256'],
            applied_rows=len(audit), skipped_nontrading_rows=len(skipped_approved),
            skipped_rows_sha256=digest(output/'skipped_approved_corrections.csv'))
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return manifest


def valued_rows(result):
    closed=result.trades.copy()
    closed['valuation_return']=closed.net_return
    closed['valuation_status']='closed'
    opened=result.positions.copy()
    opened['valuation_return']=opened.unrealized_return
    opened['valuation_status']='open'
    return pd.concat([closed,opened],ignore_index=True)


def stats(frame):
    r=frame.valuation_return
    return dict(all_trade_count=len(frame),all_mean_strategy_return=r.mean(),median_return=r.median(),
                positive_return_rate=r.gt(0).mean(),holding_mean=frame.holding_sessions.mean(),
                holding_median=frame.holding_sessions.median(),open_count=int(frame.valuation_status.eq('open').sum()))


def run(data,output,previous):
    output=Path(output)
    cfg=Config(start='2016-01-04',end='2020-12-30',time_exit_enabled=False)
    manifest,execution,inputs,classes,calendar,hashes=load_dataset(data,require_approved_universe=False,mode='exploratory',for_execution=True)
    if not manifest.get('trading_calendar'):
        raise ValueError('休場日を除外した入力をprepareで新規作成してください')
    if set(execution)!=set(STOCK_TICKERS):
        raise ValueError('Additional run requires the unchanged 50-stock universe')
    # RS benchmark is the separately cached 1306.T screener series, not a substituted fund.
    benchmark_path=Path(data).parent/'source'/'signals_1306.T.csv'
    reference_frame=pd.read_csv(benchmark_path,index_col='Date',parse_dates=['Date'],float_precision='round_trip')
    reference_frame, _ = split_trading_rows(reference_frame)
    reference=reference_frame.Close
    if digest(benchmark_path)!=manifest['source_hashes'][benchmark_path.name]:
        raise ValueError('RS benchmark hash differs')
    validate_execution_calendar(execution, inputs, calendar, reference)
    output.mkdir(parents=True,exist_ok=False)
    status=dict(status='running',settings=asdict(cfg),source_hashes=hashes,trading_calendar=calendar_metadata(),
                benchmark_rs_hash=digest(benchmark_path),kind='market',execution_mode='exploratory',
                horizons=list(HORIZONS),stops=list(STOPS),stock_count=50,etf_status='deferred_pending_data_quality',
                all_country_comparison=False,all_country_reason='Fund did not exist for full period',
                limitations=['現在の固定銘柄による生存者・選択バイアス','独立取引と期間重複・資金上限なし',
                             '調整済み価格・仮想端株','微小な高安丸め差の承認済み補正',
                             '追加過去期間であり将来の再現性を証明しない'],
                code_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip())
    code=output/'code';code.mkdir()
    for path in [*Path('stock_screener').glob('*.py'),Path(__file__)]:
        shutil.copy2(path,code/path.name)
    status['code_hashes']={p.name:digest(p) for p in code.iterdir()}
    status_path=output/'run.json'
    status_path.write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')
    entry_rows=[];exit_rows=[];group_rows=[]
    started=time.monotonic()
    for strategy in STRATEGIES:
        flags={ticker:screening_signals(frame,strategy,reference) for ticker,frame in inputs.items()}
        folder=output/strategy;folder.mkdir()
        signals_dir=folder/'signals';signals_dir.mkdir()
        for ticker,flag in flags.items():
            save(flag,signals_dir/f'{ticker}.csv.gz',index=True)
        for horizon in HORIZONS:
            outcomes,baseline,excluded=fixed_horizon(execution,flags,calendar,None,horizon,cfg)
            selected=outcomes[outcomes.is_signal]
            entry_rows.append(dict(strategy=strategy,horizon=horizon,scope='individual_stocks',**entry_statistics(selected)))
            save(outcomes,folder/f'fixed_{horizon}_all_ready_days.csv.gz')
            save(baseline,folder/f'fixed_{horizon}_baseline.csv')
            save(excluded,folder/f'fixed_{horizon}_exclusions.csv.gz')
            for field in ('ticker','entry_year'):
                summary=[dict(strategy=strategy,horizon=horizon,**{field:key},**entry_statistics(group))
                         for key,group in selected.groupby(field)]
                save(pd.DataFrame(summary),folder/f'fixed_{horizon}_by_{field}.csv')
        for stop in STOPS:
            for policy in 'ABCD':
                target=folder/f'{policy}_stop_{round(stop*100):02d}';target.mkdir()
                result=run_independent_backtest(execution,flags,strategy,policy,calendar,classes,
                    Config(start=cfg.start,end=cfg.end,initial_stop=stop,time_exit_enabled=False),ratchet_reference_stop=.08)
                valued=valued_rows(result)
                for name,frame in [('trades',result.trades),('positions',result.positions),('rejections',result.rejections),('valuations',valued)]:
                    save(frame,target/f'{name}.csv.gz')
                if result.order_updates is not None:
                    save(result.order_updates,target/'order_updates.csv.gz')
                exit_rows.append(dict(strategy=strategy,policy=policy,stop=stop,scope='individual_stocks',**stats(valued)))
                valued['entry_year']=pd.to_datetime(valued.entry_date).dt.year
                for field in ('ticker','entry_year'):
                    for key,group in valued.groupby(field):
                        group_rows.append(dict(strategy=strategy,policy=policy,stop=stop,**{field:key},**stats(group)))
                print(f'{strategy} {policy} stop={stop:.0%}: {len(exit_rows)}/140 ({time.monotonic()-started:.0f}s)',flush=True)
        save(pd.DataFrame(entry_rows),output/'entry_summary.csv')
        save(pd.DataFrame(exit_rows),output/'exit_summary.csv')
        save(pd.DataFrame(group_rows),output/'exit_by_year_or_ticker.csv')
    old=pd.read_csv(previous,encoding='utf-8-sig')
    old=old[old.scope=='individual_stocks']
    columns=['strategy','horizon','count','mean_return','timing_advantage','mean_mae','mean_mfe']
    comparison=pd.DataFrame(entry_rows)[columns].merge(old[columns],on=['strategy','horizon'],suffixes=('_2016_2020','_2021_2025'),validate='one_to_one')
    save(comparison,output/'period_comparison.csv')
    status.update(status='completed',entry_conditions=len(entry_rows),exit_conditions=len(exit_rows),
                  elapsed_seconds=time.monotonic()-started)
    status_path.write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')
    return comparison

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    prep=sub.add_parser('prepare');prep.add_argument('--source',required=True);prep.add_argument('--output',required=True)
    prep.add_argument('--approve-rounding',action='store_true',help='固定の承認レコードと一致する微小補正だけを適用')
    execute=sub.add_parser('run');execute.add_argument('--data',required=True);execute.add_argument('--output',required=True)
    execute.add_argument('--previous',required=True)
    args=parser.parse_args()
    if args.command=='prepare':prepare(args.source,args.output,args.approve_rounding)
    else:run(args.data,args.output,args.previous)
