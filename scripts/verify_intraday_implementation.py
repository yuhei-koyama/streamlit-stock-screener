#!/usr/bin/env python3
"""分足実装の自動テスト・保存済み限定監査・信号準備状態を照合する。"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import numpy as np
import pandas as pd

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
from scripts.run_intraday_backtest import load_pilot
from stock_screener.backtest_signals import STRATEGIES
from stock_screener.exploratory_data import digest
from stock_screener.intraday import IntradayConfig,STOPS,intraday_signals
from stock_screener.market_research import write_json


def run(data,audit,output):
    data,audit,output=Path(data),Path(audit),Path(output)
    if output.exists():
        raise ValueError('新しい検算先が必要です')
    metadata=json.loads((audit/'run.json').read_text(encoding='utf-8'))
    if metadata['status']!='completed' or not metadata['audit_only'] or metadata['completed_cases']!=8:
        raise ValueError('8条件の限定監査が必要です')
    for name,expected in metadata['code_hashes'].items():
        if digest(REPO/name)!=expected or digest(audit/'code'/name)!=expected:
            raise ValueError(f'実行コードと現在のコードが不一致です {name}')
    for name,expected in metadata['input']['input_hashes'].items():
        if digest(data/'collection-02'/name)!=expected:
            raise ValueError(f'入力が実行後に変わっています {name}')
    output.mkdir(parents=True)
    command=[sys.executable,'-m','unittest','discover','-s','tests','-v']
    with (output/'unit-tests.log').open('w',encoding='utf-8') as log:
        process=subprocess.run(command,cwd=REPO,stdout=log,stderr=subprocess.STDOUT,timeout=300,
                               env={**os.environ,'PYTHONIOENCODING':'utf-8'})
    log=(output/'unit-tests.log').read_text(encoding='utf-8')
    matched=re.search(r'Ran (\d+) tests?',log)
    tests_run=int(matched.group(1)) if matched else 0
    write_json(output/'unit-tests.json',dict(tests_run=tests_run,command=command,
        exit_code=process.returncode,successful=process.returncode==0))
    if process.returncode!=0 or tests_run==0:
        raise AssertionError('自動テストが失敗しました。unit-tests.logを参照してください')
    prices,daily,auxiliary,splits,days,nav,provenance=load_pilot(data)
    evaluation=pd.DatetimeIndex(provenance['evaluation_sessions'])
    coverage=[]
    for strategy in STRATEGIES:
        flags=intraday_signals(prices,strategy,days,splits)
        for ticker,frame in flags.items():
            frame=frame.loc[frame.index.tz_localize(None).normalize().isin(evaluation)]
            coverage.append(dict(strategy_id=strategy,ticker=ticker,
                role='RS参照' if ticker=='1306.T' else '売買対象',
                evaluation_bars=len(frame),ready_bars=int(frame.ready.sum()),
                buy_signal_bars=int((frame.ready&frame.buy_signal).sum()),
                sell_signal_bars=int((frame.ready&frame.sell_signal).sum()),
                segments=int(frame.segment_id.nunique()),latest_ready=bool(frame.ready.iloc[-1])))
    pd.DataFrame(coverage).to_csv(output/'signal_coverage.csv',index=False)
    cases=[]
    for window in ('full_session','morning_0900_0930'):
        for strategy in STRATEGIES:
            for policy in ('A','B','C','D'):
                for stop in STOPS:
                    cfg=IntradayConfig(start=str(evaluation[0].date()),end=str(evaluation[-1].date()),
                                       window=window,initial_stop=stop)
                    cases.append(dict(window=window,strategy_id=strategy,exit_policy=policy,initial_stop=cfg.initial_stop))
    if len(cases)!=280 or pd.DataFrame(cases).duplicated().any():
        raise AssertionError('本実行の設定組合せに欠損・重複があります')
    pd.DataFrame(cases).to_csv(output/'planned_cases.csv',index=False)
    checks=[]
    for path in sorted(audit.glob('*/*/*/metrics.json')):
        folder=path.parent
        trades=pd.read_csv(folder/'trades.csv',float_precision='round_trip')
        equity=pd.read_csv(folder/'daily.csv',index_col=0,float_precision='round_trip')
        config=json.loads((folder/'config.json').read_text(encoding='utf-8'))
        for filename,column in [('all_country_comparison.csv','all_country_nav'),
                                ('topix_etf_comparison.csv','topix_etf_nav')]:
            comparison=pd.read_csv(folder/filename,index_col=0)
            if (len(comparison)!=len(equity) or not comparison.index.equals(equity.index)
                    or not np.isfinite(comparison[column]).all()
                    or not np.allclose(comparison.strategy_nav,equity.nav,rtol=1e-10,atol=.01)):
                raise AssertionError(f'比較対象の保存値が不一致です {filename}')
        window=config['window']
        flat=bool(equity.positions.eq(0).all())
        identity=np.isclose(equity.nav.iloc[-1],config['initial_cash']+trades.net_pnl.sum(),rtol=1e-10,atol=.01)
        if not flat or not identity or not np.allclose(equity.nav,equity.cash,rtol=1e-10,atol=.01):
            raise ArithmeticError('限定監査の当日手仕舞いまたは資金会計が不一致です')
        if len(trades):
            entry=pd.to_datetime(trades.entry_time)
            exit_time=pd.to_datetime(trades.exit_time)
            signal_start=pd.to_datetime(trades.signal_bar_start)
            signal_time=pd.to_datetime(trades.signal_time)
            if not entry.equals(signal_time) or not (entry==signal_start+pd.Timedelta(5,unit='min')).all():
                raise AssertionError('確定後の次足始値注文と一致しません')
            if not (entry.dt.date==exit_time.dt.date).all():
                raise AssertionError('翌営業日への持ち越しがあります')
            deadline='09:30' if window=='morning_0900_0930' else '15:30'
            if not exit_time.dt.strftime('%H:%M').le(deadline).all():
                raise AssertionError('窓終了より後の売却があります')
        checks.append(dict(case=folder.relative_to(audit).as_posix(),accounting_passed=True,
                           all_sessions_flat=True,next_open_causality_passed=True,trade_count=len(trades)))
    if len(checks)!=8:
        raise ValueError('限定監査の8条件が揃っていません')
    pd.DataFrame(checks).to_csv(output/'audit_checks.csv',index=False)
    for name,expected in metadata['code_hashes'].items():
        if digest(REPO/name)!=expected:
            raise ValueError(f'検算中にコードが変わっています {name}')
    verification=dict(status='passed',tests_run=tests_run,planned_full_cases=280,
        audited_real_cases=8,all_audit_sessions_flat=True,all_audit_accounting_passed=True,
        all_audit_entries_after_confirmed_signal=True,
        source_audit_run_sha256=digest(audit/'run.json'),code_hashes=metadata['code_hashes'],
        verification_code_sha256=digest(Path(__file__)),
        source_result_hashes={p.relative_to(audit).as_posix():digest(p) for p in audit.rglob('*') if p.is_file()},
        output_hashes={p.name:digest(p) for p in output.iterdir() if p.is_file()})
    write_json(output/'verification.json',verification)
    print({key:verification[key] for key in ('status','tests_run','planned_full_cases','audited_real_cases')})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',required=True)
    parser.add_argument('--audit',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    run(args.data,args.audit,args.output)
