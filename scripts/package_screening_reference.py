#!/usr/bin/env python3
"""保存済み主検証の集計と純資産系列を検索アプリへ同梱する。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import uuid

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from stock_screener.backtest_signals import STRATEGIES
from stock_screener.entry_exit_diagnosis import STOPS


def package(source, output):
    source, output = Path(source), Path(output)
    destination = output
    metadata = json.loads((source/'run.json').read_text(encoding='utf-8'))
    if metadata.get('status') != 'completed' or metadata.get('completed_cases') != 560:
        raise ValueError('全560条件が完了した入力が必要です')
    version = hashlib.sha256((source/'run.json').read_bytes()+(source/'summary.csv').read_bytes()+Path(__file__).read_bytes()).hexdigest()[:20]
    summary = pd.read_csv(source/'summary.csv', float_precision='round_trip')
    expected = {(period,group,strategy,policy,stop) for period in ('2016_2020','2021_2025')
                for group in ('stock','etf') for strategy in STRATEGIES for policy in 'ABCD' for stop in STOPS}
    actual = set(summary[['period','asset_class','strategy_id','exit_policy','initial_stop']].itertuples(index=False,name=None))
    if len(summary)!=560 or actual!=expected:
        raise ValueError('同梱する560条件に欠損・重複があります')
    destination.mkdir(parents=True,exist_ok=True)
    published = destination/version
    if published.exists():
        proof = json.loads((published/'source.json').read_text(encoding='utf-8'))
        for name,expected_hash in proof['output_hashes'].items():
            if hashlib.sha256((published/name).read_bytes()).hexdigest()!=expected_hash:
                raise ValueError('既存の同梱版が変更されています')
    else:
        output = destination/('.staging-'+uuid.uuid4().hex)
        output.mkdir()
        _write_bundle(source,output,summary,metadata)
        output.rename(published)
    temporary = destination/('active.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps({'bundle':version,'manifest_sha256':hashlib.sha256((published/'source.json').read_bytes()).hexdigest()})+'\n',encoding='utf-8')
    temporary.replace(destination/'active.json')


def _write_bundle(source,output,summary,metadata):
    curves = []
    for row in summary.itertuples():
        folder = source/row.period/row.asset_class/row.strategy_id/f'{row.exit_policy}_stop_{row.initial_stop:.2f}'
        config = json.loads((folder/'config.json').read_text(encoding='utf-8'))
        if config['commission']!=metadata.get('commission',.001) or config['slippage']!=metadata.get('slippage',.0005):
            raise ValueError('条件別の費用と実行記録が一致しません')
        trades = pd.read_csv(folder/'trades.csv', usecols=['net_pnl','net_return'])
        summary.loc[row.Index,'average_net_pnl'] = trades.net_pnl.mean()
        summary.loc[row.Index,'average_net_return'] = trades.net_return.mean()
        curve = pd.read_csv(folder/'all_country_comparison.csv', index_col=0, parse_dates=True)
        curve.index.name = 'date'
        curve = curve.reset_index()
        curve['case_id'] = f'{row.period}/{row.asset_class}/{row.strategy_id}/{row.exit_policy}/{row.initial_stop:.2f}'
        curves.append(curve[['date','case_id',*([c for c in ('strategy_nav','all_country_nav') if c in curve])]])
    summary['case_id'] = [f'{r.period}/{r.asset_class}/{r.strategy_id}/{r.exit_policy}/{r.initial_stop:.2f}' for r in summary.itertuples()]
    summary['commission'] = config['commission']
    summary['slippage'] = config['slippage']
    summary.to_csv(output/'summary.csv', index=False, float_format='%.17g')
    pd.concat(curves, ignore_index=True).to_csv(output/'curves.csv.gz', index=False, compression={'method':'gzip','mtime':0}, float_format='%.10g')
    (output/'source.json').write_text(json.dumps(dict(
        source_run_sha256=hashlib.sha256((source/'run.json').read_bytes()).hexdigest(),
        source_summary_sha256=hashlib.sha256((source/'summary.csv').read_bytes()).hexdigest(),
        commission=config['commission'], slippage=config['slippage'], initial_cash=config['initial_cash'],
        prepared_count=metadata['prepared_count'],universe_hash=metadata['universe_hash'],
        packaging_code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        purchase_budget=config['purchase_budget'], rows=len(summary),
        limitations=['固定された現在上場銘柄の生存者・選択バイアス','仮想端数株・費用込み10万円枠','初期の検索フィルター追加後は未検証'],
        output_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.name in ('summary.csv','curves.csv.gz')}
    ),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    package(args.source,args.output)
