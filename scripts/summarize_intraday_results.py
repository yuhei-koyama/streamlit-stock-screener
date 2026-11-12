#!/usr/bin/env python3
"""保存済み280条件の会計・時刻・比較系列を照合し、日本語で集計する。"""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts.run_intraday_backtest import load_pilot
from stock_screener.backtest_signals import STRATEGIES
from stock_screener.exploratory_data import digest
from stock_screener.intraday import STOPS
from stock_screener.linda_versions import VERSIONS
from stock_screener.market_research import write_json


NAMES = {key: value[0] for key, value in VERSIONS.items()} | {
    'minervini_screen_v1': 'ミネルヴィニ派生版',
    'rsi2_screen_v1': 'RSI2', 'rsi14_screen_v1': 'RSI14'}
WINDOWS = {'full_session': '通常版', 'morning_0900_0930': '朝9時30分手仕舞い版'}


def close(actual, expected, atol=.01):
    if not np.allclose(actual, expected, rtol=1e-10, atol=atol, equal_nan=False):
        raise ArithmeticError('保存値の独立照合が不一致です')


def run(data, source, output):
    data, source, output = Path(data), Path(source), Path(output)
    state = json.loads((source / 'run.json').read_text(encoding='utf-8'))
    if state['status'] != 'completed' or state['audit_only'] or state['completed_cases'] != 280:
        raise ValueError('完了した全280条件が必要です')
    if output.exists():
        raise ValueError('新しい保存先を指定してください')
    for name, expected in state['code_hashes'].items():
        if digest(REPO / name) != expected or digest(source / 'code' / name) != expected:
            raise ValueError(f'実行コードの同一性が不一致です {name}')
    prices, daily_prices, auxiliary, splits, days, nav, provenance = load_pilot(data)
    summary = pd.read_csv(source / 'summary.csv', float_precision='round_trip')
    keys = ['window', 'strategy_id', 'exit_policy', 'initial_stop']
    expected = {(w, s, p, stop) for w in WINDOWS for s in STRATEGIES
                for p in 'ABCD' for stop in STOPS}
    if len(summary) != 280 or set(summary[keys].itertuples(index=False, name=None)) != expected:
        raise ValueError('280条件の組合せに重複・不足があります')
    sessions = pd.DatetimeIndex(provenance['evaluation_sessions'])
    coverage = pd.read_csv(data / 'implementation-verification-05/signal_coverage.csv')
    coverage = coverage[coverage.role.eq('売買対象')].groupby('strategy_id')[
        ['evaluation_bars', 'ready_bars', 'buy_signal_bars']].sum()
    checks = []
    for row in summary.itertuples():
        folder = source / row.window / row.strategy_id / f'{row.exit_policy}_stop_{row.initial_stop:.3f}'
        config = json.loads((folder / 'config.json').read_text(encoding='utf-8'))
        trades = pd.read_csv(folder / 'trades.csv', float_precision='round_trip')
        equity = pd.read_csv(folder / 'daily.csv', index_col=0, parse_dates=True, float_precision='round_trip')
        bars = pd.read_csv(folder / 'bars.csv', index_col=0, float_precision='round_trip')
        contributions = pd.read_csv(folder / 'contributions.csv', float_precision='round_trip')
        if not equity.index.equals(sessions) or not equity.positions.eq(0).all():
            raise AssertionError('評価日または当日手仕舞いが不一致です')
        if len(trades) != row.trade_count or not pd.read_csv(folder / 'positions.csv').empty:
            raise AssertionError('取引件数または期末保有が不一致です')
        initial = config['initial_cash']
        if len(trades):
            entry = pd.to_datetime(trades.entry_time)
            exit_time = pd.to_datetime(trades.exit_time)
            signal = pd.to_datetime(trades.signal_bar_start)
            if not (entry == signal + pd.Timedelta(5, unit='min')).all():
                raise AssertionError('確定後の次足注文に不一致があります')
            if not (entry.dt.date == exit_time.dt.date).all():
                raise AssertionError('持ち越し取引があります')
            cutoff = '09:30' if row.window == 'morning_0900_0930' else '15:30'
            if not exit_time.dt.strftime('%H:%M').le(cutoff).all():
                raise AssertionError('終了時間を超えた売却があります')
            times = entry.dt.strftime('%H:%M')
            valid = times.between('09:05', '09:25') if row.window == 'morning_0900_0930' else (
                times.between('09:05', '11:25') | times.between('12:35', '15:20'))
            if not valid.all():
                raise AssertionError('購入時間帯が不一致です')
            close(trades.net_pnl, trades.quantity * trades.exit_price * (1-config['commission']) - trades.entry_cost)
            close(trades.net_return, trades.net_pnl / trades.entry_cost, atol=1e-12)
            close(trades.net_pnl.mean(), row.average_net_pnl)
            close(trades.net_return.mean(), row.average_net_return, atol=1e-12)
            realized = trades.net_pnl.groupby(entry.dt.tz_localize(None).dt.normalize()).sum()
        else:
            realized = pd.Series(dtype=float)
        close(equity.nav, initial + realized.reindex(sessions, fill_value=0).cumsum())
        close(equity.nav, equity.cash)
        close(bars.nav, bars.cash + bars.market_value)
        close(contributions.total_pnl.sum(), row.final_nav - initial)
        close(trades.costs.sum(), row.total_costs)
        close(row.total_return, row.final_nav / initial - 1, atol=1e-12)
        close(row.max_drawdown, (bars.nav / bars.nav.cummax() - 1).min(), atol=1e-12)
        if len(pd.read_csv(folder / 'quality_events.csv')) != row.quality_event_count:
            raise AssertionError('品質イベント件数が不一致です')
        if bars.cash.lt(-.000001).any():
            raise AssertionError('現金が負になっています')
        for name, reference, column in [
            ('all_country', nav, 'all_country_nav'),
            ('topix_etf', daily_prices['1306.T']['Adj Close'], 'topix_etf_nav')]:
            saved = pd.read_csv(folder / f'{name}_comparison.csv', index_col=0, parse_dates=True)
            reference = reference.reindex(sessions)
            if not saved.index.equals(sessions):
                raise AssertionError('比較日付が不一致です')
            close(saved[column], initial * reference / reference.iloc[0])
            close(saved.strategy_nav, equity.nav)
        checks.append(dict(window=row.window, strategy_id=row.strategy_id, exit_policy=row.exit_policy,
                           initial_stop=row.initial_stop, passed=True, daily_rows=len(equity), trade_count=len(trades)))
    output.mkdir(parents=True)
    pd.DataFrame(checks).to_csv(output / 'independent_checks.csv', index=False)
    summary['strategy_name'] = summary.strategy_id.map(NAMES)
    summary['increase_yen'] = summary.final_nav - provenance['settings']['initial_cash']
    summary = summary.join(coverage, on='strategy_id')
    summary['no_trade'] = summary.trade_count.eq(0)
    summary['rank'] = summary.groupby('window').final_nav.rank(ascending=False, method='min').astype(int)
    summary.sort_values(['window', 'final_nav', 'strategy_id', 'exit_policy', 'initial_stop'],
                        ascending=[True, False, True, True, True]).to_csv(output / 'all_conditions.csv', index=False)
    lines = ['# 5分足デイトレ・全280条件の参考試算', '',
             '2026年9月24日〜11月11日、33営業日、固定20個別株。各条件の初期資金1,000万円、費用込み10万円枠、片道手数料0.1%、成行スリッページ0.05%、仮想端数株、税引前。', '',
             '通常版140条件と朝9時30分までの140条件を完了した。独立照合で全280条件の33営業日の当日手仕舞い、現金・純資産・決済損益、費用、注文時刻、比較系列が一致した。', '',
             '## 結果の読み方', '',
             '主順位は期末資産額。全条件CSVには0取引の現金保有条件も含む。同額は同順位。以下は取引実績のある条件の上位10件と同額保有の比較対象を表示する。期待値は決済済み1取引の平均純損益で、将来の予測値には過去の標本平均を使用する前提が加わる。', '',
             '保有中欠損を含む条件には「経路欠損あり」と付ける。ミネルヴィニは準備完了0本、RSI2/14は全日合計42,900足中240足だけ判定可能で、手法比較のための取引機会が限られる。全条件が同じ欠損時再準備ルールを使用する。', '']
    rank_frames = []
    for window, title in WINDOWS.items():
        group = summary[summary.window.eq(window)].copy()
        base = group.iloc[0]
        benchmarks = []
        for name, prefix in [('オルカン', ''), ('1306.T同額保有', 'topix_')]:
            value = 10_000_000 * (1 + base[f'{prefix}benchmark_total_return'])
            benchmarks.append(dict(window=window, strategy_name=name, final_nav=value,
                                   total_return=base[f'{prefix}benchmark_total_return'], increase_yen=value-10_000_000,
                                   trade_count=np.nan, average_net_pnl=np.nan, average_net_return=np.nan,
                                   exit_policy='', initial_stop=np.nan, quality_status='benchmark_hold'))
        ranked = pd.concat([group, pd.DataFrame(benchmarks)], ignore_index=True)
        ranked['rank_with_benchmarks'] = ranked.final_nav.rank(ascending=False, method='min').astype(int)
        ranked = ranked.sort_values('final_nav', ascending=False, kind='stable')
        ranked.to_csv(output / f'ranking_{window}.csv', index=False)
        rank_frames.append(ranked)
        lines += [f'## {title}', '',
                  '| 全体順位 | 入口・比較対象 | 出口 | 損切り | 期末資産 | 損益率 | 件数 | 1取引平均損益 | 品質 |',
                  '| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |']
        active = ranked[ranked.trade_count.gt(0)].head(10)
        shown = pd.concat([active, ranked[ranked.quality_status.eq('benchmark_hold')]]).sort_values('final_nav', ascending=False)
        for item in shown.itertuples():
            benchmark = item.quality_status == 'benchmark_hold'
            stop = '—' if benchmark else f'{item.initial_stop:.1%}'
            count = '—' if benchmark else str(int(item.trade_count))
            average = '—' if benchmark else f'{item.average_net_pnl:+,.0f}円'
            quality = '期間保有' if benchmark else ('経路欠損あり' if item.quality_event_count else '観測範囲内')
            lines.append(f'| {item.rank_with_benchmarks} | {item.strategy_name} | {item.exit_policy} | {stop} | {item.final_nav:,.0f}円 | {item.total_return:+.3%} | {count} | {average} | {quality} |')
        lines += ['', f'取引あり{int(group.trade_count.gt(0).sum())}条件、0取引{int(group.no_trade.sum())}条件。保有中欠損等の参考条件{int(group.quality_event_count.gt(0).sum())}件。', '']
    pd.concat(rank_frames, ignore_index=True).to_csv(output / 'rankings_with_benchmarks.csv', index=False)
    best = summary[summary.trade_count.gt(0)].sort_values('final_nav', ascending=False).groupby(['window', 'strategy_id']).head(1)
    best.to_csv(output / 'best_by_entry.csv', index=False)
    summary.sort_values('final_nav', ascending=False).groupby(['window', 'strategy_id', 'exit_policy']).head(1).to_csv(
        output / 'best_by_entry_exit.csv', index=False)
    lines += ['## 比較と約定の前提', '',
              'オルカンと1306.Tは評価初日終値を初期資金に揃えた期間保有。戦略には初日の場中損益も含む。1306.Tは保存済みYahoo調整終値で、分配・分割調整は配信値に依存する。比較対象の売買費用・投資家税は0。', '',
              '損切り・利確を時間手仕舞いより先に処理し、同一足の両到達は損切り優先。足内の実際の到達時刻は足末で近似する。朝手仕舞いは9時25分足終値、大引けは同日日足終値を使用する。正の足出来高による約定可能性の確認、11時30分補助価格の11時35分確認を含む価格近似。', '',
              '短期CAGRは参考値。固定20銘柄・33営業日・多数条件からの選択と相場局面の偏りを含む。0取引で現金を保った条件と、欠損による指標準備不足を記録している。日末現金比率は当日手仕舞いにより100%となる。', '',
              '## 保存先', '',
              'all_conditions.csvに全280条件と準備本数、ranking_*.csvに比較対象込みの期末資産順位、best_by_entry.csvに取引ありの入口別最高条件、best_by_entry_exit.csvに入口・出口別最高条件を保存した。入口別最高条件は同じ標本内で出口・損切りを選んだ探索値。independent_checks.csvは全条件の独立照合。', '',
              '計算原本は../primary-run-01/。各条件に全取引、足別・日次資産、注文変更、見送り、品質イベント、寄与、設定と比較系列がある。']
    (output / 'REPORT.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    write_json(output / 'verification.json', dict(status='passed', checked_cases=280,
        source_run_sha256=digest(source / 'run.json'), source_summary_sha256=digest(source / 'summary.csv'),
        code_sha256=digest(Path(__file__)), source_code_hashes=state['code_hashes'],
        output_hashes={p.name: digest(p) for p in output.iterdir() if p.is_file()}))
    print('全280条件の独立照合・ランキング・日本語報告を保存しました')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    run(args.data, args.source, args.output)
