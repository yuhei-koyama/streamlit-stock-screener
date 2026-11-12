#!/usr/bin/env python3
"""同一入力の手数料変更前後を照合し、全条件の差を日本語で保存する。"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from stock_screener.screening_workspace import NAMES

KEYS = ['period','asset_class','strategy_id','exit_policy','initial_stop']


def compare(previous,current,output):
    previous,current,output = Path(previous),Path(current),Path(output)
    old = json.loads((previous/'run.json').read_text(encoding='utf-8'))
    new = json.loads((current/'run.json').read_text(encoding='utf-8'))
    for value in (old,new):
        if value['status']!='completed' or value['completed_cases']!=560:
            raise ValueError('両方の全560条件が必要です')
    if old['input_manifest_sha256']!=new['input_manifest_sha256'] or old['input_hashes']!=new['input_hashes']:
        raise ValueError('比較する入力が異なります')
    if new['commission']!=0 or new['slippage']!=.0005:
        raise ValueError('新しい費用条件が承認値と異なります')
    normalized_code = {name.replace('\\','/'):value for name,value in new['code_hashes'].items()}
    for name,expected in old['code_hashes'].items():
        name = name.replace('\\','/')
        if name.startswith('stock_screener/') and normalized_code.get(name)!=expected:
            raise ValueError('費用変更以外の共通計算式に変更があります。'+name)
    before = pd.read_csv(previous/'summary.csv',float_precision='round_trip')
    after = pd.read_csv(current/'summary.csv',float_precision='round_trip')
    table = before.merge(after,on=KEYS,suffixes=('_before','_after'),validate='one_to_one')
    if len(table)!=560:
        raise ValueError('条件の組合せが一致しません')
    table['terminal_value_difference_yen'] = table.final_nav_after-table.final_nav_before
    table['cagr_difference_points'] = (table.cagr_after-table.cagr_before)*100
    table['trade_count_difference'] = table.trade_count_after-table.trade_count_before
    table['open_position_difference'] = table.open_positions_after-table.open_positions_before
    table['strategy_name'] = table.strategy_id.map(NAMES)
    output.mkdir(parents=True,exist_ok=False)
    table.to_csv(output/'all_fee_differences.csv',index=False,float_format='%.17g')
    lines = ['# 手数料0%への変更後の再検証', '',
        '同じ4,022銘柄の保存済み入力で全560条件を再実行した。片道手数料0.1%から0%へ変更し、成行スリッページ0.05%、初期資金各1,000万円、費用込み10万円購入枠などを揃えた。現行費用の仮定を2016〜2020年・2021〜2025年の両期間へ適用した税引前の探索結果。', '',
        '費用変更により購入株数・資金余力と後続の売買が変化する。以下は売買処理を再実行した差。', '',
        '| 期間 | 資産区分 | 条件 | 変更前期末資産 | 変更後期末資産 | 増減 | 変更後年率 | オルカン年率 |',
        '| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |']
    for (period,group),part in table.groupby(['period','asset_class']):
        best = part.sort_values('final_nav_after',ascending=False).iloc[0]
        benchmark = '設定前を含む' if pd.isna(best.benchmark_cagr_after) else f'{best.benchmark_cagr_after:.2%}'
        title = f'{best.strategy_name}・{best.exit_policy}・損切り{best.initial_stop:.0%}'
        lines.append(f'| {period.replace("_","〜")} | {"個別株" if group=="stock" else "ETF"} | {title} | {best.final_nav_before:,.0f}円 | {best.final_nav_after:,.0f}円 | {best.terminal_value_difference_yen:+,.0f}円 | {best.cagr_after:.2%} | {benchmark} |')
    available = table[table.period.eq('2021_2025') & table.benchmark_status_after.eq('compared')]
    lines += ['',f'2021〜2025年の比較可能条件は{len(available)}件。オルカンを年率で上回る条件は変更前{int(available.excess_cagr_before.gt(0).sum())}件、変更後{int(available.excess_cagr_after.gt(0).sum())}件。', '',
        f'決済件数が変化した条件は{int(table.trade_count_difference.ne(0).sum())}件、期末保有数が変化した条件は{int(table.open_position_difference.ne(0).sum())}件。', '',
        '## 全条件の参照', '',
        'all_fee_differences.csvに全560条件の期末資産・年率・最大ドローダウン・決済件数・期末保有と差を保存した。新しい本実行の取引・純資産・資金会計は再計算フォルダーに保存する。', '',
        '固定上場一覧の生存者・選択バイアス、価格補正・保守的な行除外、仮想端数株、同じ標本内の多数条件比較を含む。現在の検索用流動性フィルター・購入予算・実売買単位・OR／ANDを追加した成績は別の検証を要する。']
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    proof = dict(status='passed',conditions=560,
        previous_run_sha256=hashlib.sha256((previous/'run.json').read_bytes()).hexdigest(),
        current_run_sha256=hashlib.sha256((current/'run.json').read_bytes()).hexdigest(),
        comparison_code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        input_manifest_sha256=new['input_manifest_sha256'],
        output_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()})
    (output/'verification.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('全560条件の費用変更前後の比較を保存しました')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous',required=True)
    parser.add_argument('--current',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    compare(args.previous,args.current,args.output)
