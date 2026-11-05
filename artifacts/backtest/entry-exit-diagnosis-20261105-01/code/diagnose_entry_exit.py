#!/usr/bin/env python3
"""保存済み日本株データで承認済み入口・出口診断を実行し保存する。"""
import argparse
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from stock_screener.backtest import Config
from stock_screener.backtest_io import load_dataset
from stock_screener.backtest_signals import STRATEGIES, screening_signals
from stock_screener.benchmark_comparison import load_all_country_nav, compare_including_open, valuation_statistics
from stock_screener.independent_backtest import run_independent_backtest
from stock_screener.entry_exit_diagnosis import HORIZONS, STOPS, fixed_horizon, entry_statistics, recovery_diagnostics
from stock_screener.universe import STOCK_TICKERS


def save(frame, path):
    frame.to_csv(path, index=False, encoding='utf-8-sig', float_format='%.17g')


def scope(ticker):
    return 'individual_stocks' if ticker in STOCK_TICKERS else 'etfs'


def main(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    cfg = Config(time_exit_enabled=False)
    manifest, execution, inputs, classes, calendar, hashes = load_dataset(args.data, mode='exploratory')
    nav = load_all_country_nav(args.fund_csv)
    status = dict(status='running', specification='entry-exit-diagnosis-v1/1.0-approved',
                  data=args.data, fund_csv=args.fund_csv, input_hashes=hashes,
                  fund_hash=sha256(Path(args.fund_csv).read_bytes()).hexdigest(),
                  settings=asdict(cfg), horizons=HORIZONS, stops=STOPS,
                  diagnostic_reference_stop=.08, primary='50 individual stocks',
                  code_commit=subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip(),
                  limitations=['探索・診断', '現在の固定銘柄一覧による生存者・選択バイアス',
                               '同一銘柄の重複取引と期間重複', '調整価格・仮想端株',
                               '期末保有は未実現損益・売却費用未計上', '独立取引平均は年率ではない'])
    code = output/'code'
    code.mkdir()
    for path in [*Path('stock_screener').glob('*.py'), Path(__file__)]:
        shutil.copy2(path, code/path.name)
    status['code_hashes'] = {p.name:sha256(p.read_bytes()).hexdigest() for p in code.iterdir()}
    shutil.copy2('docs/specs/entry-exit-diagnosis-v1.md', output/'SPECIFICATION.md')
    shutil.copy2(Path(args.data)/'corrections.csv', output/'corrections.csv')
    shutil.copy2(Path(args.data)/'manifest.json', output/'input_manifest.json')
    shutil.copy2(args.fund_csv, output/'253425-original.csv')
    (output/'run.json').write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')
    entry_summary, exit_summary, recovery_summary, stability, concentration, audits = [], [], [], [], [], []
    start = time.monotonic()
    for strategy in STRATEGIES:
        flags = {ticker:screening_signals(frame,strategy,inputs['1306.T'].Close)
                 for ticker,frame in inputs.items()}
        folder = output/strategy
        folder.mkdir()
        signals_folder = folder/'signals'
        signals_folder.mkdir()
        for ticker, flag in flags.items():
            save(flag.reset_index(), signals_folder/f'{ticker}.csv')
        windows = {}
        for horizon in HORIZONS:
            outcomes, baseline, exclusions = fixed_horizon(execution,flags,calendar,nav,horizon,cfg)
            outcomes['scope'] = outcomes.ticker.map(scope)
            windows[horizon] = outcomes
            save(outcomes,folder/f'fixed_{horizon}_all_ready_days.csv')
            save(baseline,folder/f'fixed_{horizon}_baseline.csv')
            save(exclusions,folder/f'fixed_{horizon}_exclusions.csv')
            selected = outcomes[outcomes.is_signal]
            for name, group in selected.groupby('scope'):
                entry_summary.append(dict(strategy=strategy,horizon=horizon,scope=name,**entry_statistics(group)))
            for fields in [('scope','entry_year'),('scope','ticker')]:
                rows = [dict(zip(fields,key),**entry_statistics(group)) for key,group in selected.groupby(list(fields))]
                save(pd.DataFrame(rows),folder/f'fixed_{horizon}_by_{fields[1]}.csv')
        reference_entries = None
        for stop in STOPS:
            for policy in 'ABCD':
                label = f'{policy}_stop_{round(stop*100):02d}'
                target = folder/label
                target.mkdir()
                result = run_independent_backtest(execution,flags,strategy,policy,calendar,classes,
                    Config(time_exit_enabled=False,initial_stop=stop),ratchet_reference_stop=.08)
                valued = compare_including_open(result.trades,result.positions,nav,cfg.end)
                valued['scope'] = valued.ticker.map(scope)
                entry_ids = set(valued.trade_id)
                if reference_entries is None:
                    reference_entries = entry_ids
                assert entry_ids == reference_entries, (strategy,label,'entry mismatch')
                for name, frame in [('trades',result.trades),('positions',result.positions),
                                    ('rejections',result.rejections),('valuations',valued)]:
                    save(frame,target/f'{name}.csv')
                if result.order_updates is not None:
                    save(result.order_updates,target/'order_updates.csv')
                if stop == .08:
                    previous = Path('artifacts/backtest/holding-period-comparison-20261105-01/no-limit-run') if policy in 'AB' else Path('artifacts/backtest/ratchet-exits-20261105-01/run')
                    # 既存成果物のフォルダ命名を確認し、8%一致を必須監査する。
                    previous_folder = previous/f'{strategy}__{policy}'
                    if not previous_folder.exists():
                        previous_folder = previous/f'{strategy}_{policy}'
                    for name, frame in [('trades',result.trades),('positions',result.positions)]:
                        old = pd.read_csv(previous_folder/f'{name}.csv',float_precision='round_trip')
                        current = pd.read_csv(target/f'{name}.csv',float_precision='round_trip')
                        pd.testing.assert_frame_equal(old,current,check_dtype=False,rtol=1e-12,atol=1e-12)
                    audits.append(dict(strategy=strategy,policy=policy,baseline_8pct_match=True))
                valued['entry_year'] = pd.to_datetime(valued.entry_date).dt.year
                for name, group in valued.groupby('scope'):
                    exit_summary.append(dict(strategy=strategy,policy=policy,stop=stop,scope=name,
                                             **valuation_statistics(group),open_count=int(group.valuation_status.eq('open').sum())))
                    by_ticker = group.groupby('ticker').excess_return.sum().sort_values(ascending=False)
                    for removed in (1,3,5):
                        rest = group[~group.ticker.isin(by_ticker.head(removed).index)]
                        concentration.append(dict(strategy=strategy,policy=policy,stop=stop,scope=name,
                                                  removed_top_tickers=removed,**valuation_statistics(rest)))
                for fields in [('scope','entry_year'),('scope','ticker')]:
                    for key, group in valued.groupby(list(fields)):
                        stability.append(dict(strategy=strategy,policy=policy,stop=stop,
                                              **dict(zip(fields,key)),**valuation_statistics(group)))
                for horizon,outcomes in windows.items():
                    recovery = recovery_diagnostics(result.trades,outcomes,execution)
                    if len(recovery):
                        recovery['scope'] = recovery.ticker.map(scope)
                        save(recovery,target/f'recovery_{horizon}.csv')
                        for name,group in recovery.groupby('scope'):
                            eligible = group[group.stopped_before_horizon]
                            recovery_summary.append(dict(strategy=strategy,policy=policy,stop=stop,horizon=horizon,
                                scope=name,stopped_before_horizon=len(eligible),
                                later_close_recovered=int(eligible.later_close_recovered.sum()),
                                recovery_rate=eligible.later_close_recovered.mean() if len(eligible) else None))
                print(f'{strategy} {label} completed ({time.monotonic()-start:.0f}s)',flush=True)
        save(pd.DataFrame(entry_summary),output/'entry_summary.csv')
        save(pd.DataFrame(exit_summary),output/'exit_summary.csv')
        save(pd.DataFrame(recovery_summary),output/'recovery_summary.csv')
        save(pd.DataFrame(stability),output/'exit_by_year_or_ticker.csv')
        save(pd.DataFrame(concentration),output/'concentration.csv')
    status.update(status='completed',exit_condition_count=140,entry_condition_count=35,
                  baseline_audits=audits,elapsed_seconds=time.monotonic()-start)
    (output/'run.json').write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',required=True)
    parser.add_argument('--fund-csv',required=True)
    parser.add_argument('--output',required=True)
    main(parser.parse_args())
