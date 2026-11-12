#!/usr/bin/env python3
"""有限資金の全条件を資金会計と照合し、日本語の比較報告へ集約する。"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock_screener.backtest_signals import STRATEGIES
from stock_screener.entry_exit_diagnosis import STOPS

NAMES = {'tv_ema_stoch_v1': 'TradingView添付版', 'lbr_anti_310_adaptation_v1': 'Anti 3/10適応版',
         'lbr_anti_stochastic_v1': 'Anti 7/4/10版', 'legacy_ema_sma16_v1': '初期試作版',
         'minervini_screen_v1': 'ミネルヴィニ派生版', 'rsi2_screen_v1': 'RSI2', 'rsi14_screen_v1': 'RSI14'}


def summarize(run, data, output):
    run, data, output = Path(run), Path(data), Path(output)
    metadata_content = (run / 'run.json').read_bytes()
    metadata = json.loads(metadata_content)
    if metadata['status'] != 'completed' or metadata.get('audit_only') or metadata.get('completed_cases') != 560:
        raise ValueError('560条件を完了した本実行が必要です')
    manifest_content = (data / 'manifest.json').read_bytes()
    manifest = json.loads(manifest_content)
    if sha256(manifest_content).hexdigest() != metadata['input_manifest_sha256'] or manifest['file_hashes'] != metadata['input_hashes']:
        raise ValueError('本実行と現在の入力が一致しません')
    for name, expected in metadata['input_hashes'].items():
        if sha256((data / name).read_bytes()).hexdigest() != expected:
            raise ValueError('準備済み入力のハッシュが変わっています')
    for name, expected in metadata['code_hashes'].items():
        if sha256((run / 'code' / name.replace('\\', '/')).read_bytes()).hexdigest() != expected:
            raise ValueError('本実行のコード保存物が変わっています')
    if output.exists():
        raise ValueError('新規の集計先が必要です')
    records, checks, hashes = [], [], {}
    for path in sorted(run.glob('*/*/*/*/metrics.json')):
        folder = path.parent
        period, group, strategy, policy = folder.relative_to(run).parts
        metrics = json.loads(path.read_text(encoding='utf-8'))
        cfg = json.loads((folder / 'config.json').read_text(encoding='utf-8'))
        if cfg['commission'] != metadata.get('commission',.001) or cfg['slippage'] != metadata.get('slippage',.0005):
            raise ValueError('本実行と条件別の費用設定が不一致です')
        if cfg['initial_cash'] != 10_000_000 or cfg['purchase_budget'] != 100_000 or cfg['time_exit_enabled']:
            raise ValueError('資金または保有期限設定が不正です')
        frames = {}
        for name in ('daily', 'trades', 'positions', 'contributions'):
            file = folder / (name + '.csv')
            frames[name] = pd.read_csv(file, float_precision='round_trip')
        daily, trades, positions, contributions = (frames[name] for name in ('daily', 'trades', 'positions', 'contributions'))
        final = float(daily.nav.iloc[-1])
        initial = float(cfg['initial_cash'])
        equations = [np.allclose(daily.nav, daily.cash + daily.market_value, rtol=1e-10, atol=.01),
                     bool(daily.cash.ge(-1e-6).all()),
                     np.isclose(final, initial + trades.net_pnl.sum() + positions.unrealized_pnl.sum(), rtol=1e-10, atol=.01),
                     np.isclose(daily.cash.iloc[-1], initial + trades.net_pnl.sum() - positions.entry_cost.sum(), rtol=1e-10, atol=.01),
                     np.isclose(contributions.total_pnl.sum(), final - initial, rtol=1e-10, atol=.01),
                     np.isclose(metrics['final_nav'], final, rtol=1e-10, atol=.01),
                     bool(trades.entry_cost.eq(100_000).all()), bool(positions.entry_cost.eq(100_000).all())]
        if not all(equations):
            raise ArithmeticError('保存結果の資金会計が不一致です。' + str(folder))
        if metrics['benchmark_status'] == 'compared':
            if not np.isclose(metrics['cagr'] - metrics['benchmark_cagr'], metrics['excess_cagr'], atol=1e-12):
                raise ArithmeticError('オルカンCAGR差が不一致です')
        records.append(dict(period=period, asset_class=group, strategy_id=strategy, exit_policy=policy.split('_')[0],
                            initial_stop=cfg['initial_stop'], **metrics))
        checks.append(dict(case=folder.relative_to(run).as_posix(), accounting_passed=True,
                           trade_count=len(trades), open_positions=len(positions), final_nav=final))
        for file in folder.iterdir():
            if file.is_file():
                hashes[file.relative_to(run).as_posix()] = sha256(file.read_bytes()).hexdigest()
    expected = {(period, group, strategy, policy, stop)
                for period in ('2016_2020', '2021_2025') for group in ('stock', 'etf')
                for strategy in STRATEGIES for policy in ('A', 'B', 'C', 'D') for stop in STOPS}
    actual = {(r['period'], r['asset_class'], r['strategy_id'], r['exit_policy'], r['initial_stop']) for r in records}
    if len(records) != 560 or actual != expected:
        raise ValueError('560条件の組合せに欠損または重複があります')
    table = pd.DataFrame(records).drop(columns=['max_drawdown_period', 'longest_drawdown_period'])
    # 比較の可否は計算済みのbenchmark_statusで区分する。
    table['engine_initial_comparison_state'] = table.comparison_status
    table['comparison_status'] = table.benchmark_status
    table['comparison_availability'] = table.benchmark_status
    available = table[table.period.eq('2021_2025') & table.benchmark_status.eq('compared')]
    winners = available[available.excess_cagr.gt(0)]
    output.mkdir(parents=True)
    table.to_csv(output / 'summary.csv', index=False, float_format='%.17g')
    pd.DataFrame(checks).to_csv(output / 'accounting_checks.csv', index=False, float_format='%.17g')
    winners.sort_values('excess_cagr', ascending=False).to_csv(output / 'all_country_outperforming_cases.csv', index=False, float_format='%.17g')
    grouped = []
    for (group, strategy), frame in available.groupby(['asset_class', 'strategy_id'], sort=True):
        best = frame.loc[frame.excess_cagr.idxmax()]
        grouped.append(dict(asset_class=group, strategy_id=strategy, comparable_cases=len(frame),
                            outperforming_cases=int(frame.excess_cagr.gt(0).sum()),
                            best_exit=best.exit_policy, best_stop=best.initial_stop,
                            best_cagr=best.cagr, benchmark_cagr=best.benchmark_cagr,
                            best_excess_cagr=best.excess_cagr, best_max_drawdown=best.max_drawdown))
    pd.DataFrame(grouped).to_csv(output / 'by_entry_method.csv', index=False, float_format='%.17g')
    contributor_tables = []
    best_case_effects = []
    names = pd.DataFrame(manifest['prepared_assets'])[['ticker', 'name']]
    for group, frame in available.groupby('asset_class', sort=True):
        best = frame.loc[frame.excess_cagr.idxmax()]
        folder = run / best.period / group / best.strategy_id / f'{best.exit_policy}_stop_{best.initial_stop:.2f}'
        contributions = pd.read_csv(folder / 'contributions.csv', float_precision='round_trip').nlargest(10, 'total_pnl')
        effects = pd.read_csv(folder / 'excluded_row_effects.csv')
        held_counts = effects.groupby('ticker').held_position.sum().rename('excluded_rows_while_held')
        contributions = contributions.merge(names, on='ticker', how='left').merge(held_counts, on='ticker', how='left')
        contributions['excluded_rows_while_held'] = contributions.excluded_rows_while_held.fillna(0).astype(int)
        contributions['asset_class'] = group
        contributions['strategy_id'] = best.strategy_id
        contributions['exit_policy'] = best.exit_policy
        contributions['initial_stop'] = best.initial_stop
        contributor_tables.append(contributions)
        best_case_effects.append(dict(asset_class=group, strategy_id=best.strategy_id, exit_policy=best.exit_policy,
                                      initial_stop=best.initial_stop,
                                      excluded_rows_while_held=int(best.excluded_price_rows_while_held),
                                      excluded_rows_with_buy_rejection=int(best.excluded_price_rows_with_buy_rejection)))
    if contributor_tables:
        pd.concat(contributor_tables, ignore_index=True).to_csv(output / 'best_case_top_contributors.csv', index=False, float_format='%.17g')
    pd.DataFrame(best_case_effects).to_csv(output / 'best_case_exclusion_effects.csv', index=False)
    asset_counts = pd.DataFrame(manifest['prepared_assets']).asset_class.value_counts()
    excluded_count = manifest['approved_invalid_price_rows_excluded']
    lines = ['# 有限資金バックテストの全560条件', '',
             f"対象は個別株{asset_counts.get('stock', 0):,}銘柄・ETF{asset_counts.get('etf', 0):,}銘柄、計{manifest['prepared_count']:,}銘柄。各条件で個別株・ETFにそれぞれ初期1,000万円、費用込み10万円の固定購入枠を設定した。", '',
             f"入口7設定、出口A〜D、初期ストップ3・5・8・10・12%、2016〜2020年と2021〜2025年を検証した。保有期限なし、仮想端数株、同一銘柄の重複購入抑止、片道手数料{cfg['commission']:.2%}、成行スリッページ{cfg['slippage']:.2%}。", '',
             f"準備済み価格{manifest['price_rows_after_exclusion']:,}行を使用し、承認済み行除外{excluded_count:,}行を台帳で追跡した。除外には市場開始前の履歴や、誤データ所在が未確定のサービス間差異を含む。営業日カレンダーに沿って評価し、個別価格欠損の買い取消・売却繰越・最新価格評価を適用した。", '',
             '## 資金会計の照合', '',
             '560条件すべてで、日次純資産と現金・保有評価額、期末純資産と実現・含み損益、銘柄別寄与、購入原価固定を照合した。accounting_checks.csvに結果を保存した。', '',
             '## 2021〜2025年のオルカン比較', '',
             f'同日付の公式分配金再投資基準価額を使用した。比較可能{len(available)}条件のうち、CAGRがオルカンを上回ったのは{len(winners)}条件。古い期末評価価格がある条件は最終判定を保留する。', '',
             '| 資産区分 | 入口 | 上回った条件数 | 最大差の出口 | 損切り | 戦略CAGR | オルカンCAGR | CAGR差 | 戦略最大DD |',
             '| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |']
    for row in grouped:
        group = '個別株' if row['asset_class'] == 'stock' else 'ETF'
        lines.append(f"| {group} | {NAMES.get(row['strategy_id'], row['strategy_id'])} | {row['outperforming_cases']}/{row['comparable_cases']} | {row['best_exit']} | {row['best_stop']:.0%} | {row['best_cagr']:.2%} | {row['benchmark_cagr']:.2%} | {row['best_excess_cagr']*100:+.2f}pt | {row['best_max_drawdown']:.2%} |")
    lines += ['', '## 2016〜2020年', '',
              '実ファンド設定前を含むため、全期間のオルカン比較は欠損として保存した。戦略の純資産・CAGR・ドローダウン・年別成績は各条件へ保存した。', '',
              '## 除外行と評価範囲', '',
              '各条件のexcluded_row_effects.csvとsummary.csvに、除外日に保有中だった取引と買い見送り件数を保存した。行数の割合と損益への因果的な影響は区分して参照する。', '',
              f'現在の上場一覧による生存者・選択バイアスと、仮想端数株・調整価格・承認済み{excluded_count:,}行除外の仮定を含む探索結果。入口ごとの最大差は20通りの出口・ストップから集計した。', '',
              '集計版ではbenchmark_statusに基づいてcomparison_statusとcomparison_availabilityを揃えた。原実行の初期状態はengine_initial_comparison_stateに保存した。', '',
              '## 保存物', '',
              'summary.csvは全条件、all_country_outperforming_cases.csvは比較可能な超過条件、by_entry_method.csvは入口別の集計。best_case_top_contributors.csvは各資産区分の最大差ケースの上位寄与銘柄、best_case_exclusion_effects.csvはその除外日の保有・見送り件数。元の取引・保有・注文・純資産・寄与・回復期間・コード保存物は本実行フォルダにある。']
    lines += ['', '## 全条件の損益率と年率', '',
              '各セルは期間損益率 / CAGR。費用と期末含み損益を含む。暫定の表示は古い期末評価価格を含む条件を示す。', '',
              'サービス間差異の保守的除外、価格照合に用いた一定単位差の推定と参照先の独立性、未照合期間の制約を含む。11ETFの2025年10月24日の個別取得欠損は、承認済みの欠損規則で処理した。']
    for (period, group), frame in table.groupby(['period', 'asset_class'], sort=True):
        label = '個別株' if group == 'stock' else 'ETF'
        lines += ['', f"### {period.replace('_', '〜')}年 {label}", '',
                  '| 入口 | 出口 | 損切り3% | 損切り5% | 損切り8% | 損切り10% | 損切り12% |',
                  '| --- | --- | ---: | ---: | ---: | ---: | ---: |']
        for strategy in STRATEGIES:
            for policy in ('A', 'B', 'C', 'D'):
                cases = frame[frame.strategy_id.eq(strategy) & frame.exit_policy.eq(policy)].set_index('initial_stop')
                cells = []
                for stop in STOPS:
                    row = cases.loc[stop]
                    provisional = ' 暫定' if row.valuation_status != 'current_prices' else ''
                    cells.append(f'{row.total_return:+.2%} / {row.cagr:+.2%}{provisional}')
                lines.append('| ' + ' | '.join([NAMES.get(strategy, strategy), policy, *cells]) + ' |')
    (output / 'REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    verification = dict(status='completed', checked_cases=560, accounting_passed_cases=560,
                        comparable_2021_2025_cases=len(available), outperforming_cases=len(winners),
                        source_run_sha256=sha256(metadata_content).hexdigest(),
                        input_manifest_sha256=metadata['input_manifest_sha256'],
                        aggregation_code_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
                        source_result_hashes=hashes)
    (output / 'verification.json').write_text(json.dumps(verification, ensure_ascii=False, indent=2), encoding='utf-8')
    print({key: verification[key] for key in ('status', 'checked_cases', 'comparable_2021_2025_cases', 'outperforming_cases')})
    return verification


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--data', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    summarize(args.run, args.data, args.output)
