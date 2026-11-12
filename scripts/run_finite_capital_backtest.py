#!/usr/bin/env python3
"""有限資金の固定条件比較、または部分入力による資金会計監査を保存する。"""
import argparse
from dataclasses import asdict
from hashlib import sha256
import io
import json
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock_screener.backtest_signals import STRATEGIES, screening_signals
from stock_screener.benchmark_comparison import FUND_URL, load_all_country_nav_content, compare_portfolio_nav
from stock_screener.entry_exit_diagnosis import STOPS
from stock_screener.finite_capital import FiniteCapitalConfig, run_finite_capital_backtest, excluded_row_exposure
from stock_screener.market_research import PERIODS, frozen_assets
from stock_screener.trading_calendar import calendar_metadata, validate_execution_calendar, trading_day_mask


def write_json(path, data):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    temporary.replace(path)


def run(data, universe_root, output, audit=False, benchmark_nav=None, commission=.001, slippage=.0005):
    FiniteCapitalConfig(commission=commission, slippage=slippage)
    data, output = Path(data), Path(output)
    universe, assets = frozen_assets(universe_root)
    manifest_content = (data / 'manifest.json').read_bytes()
    manifest = json.loads(manifest_content)
    if manifest['universe_hash'] != universe['universe_hash']:
        raise ValueError('準備済み入力と固定母集団が一致しません')
    if manifest['trading_calendar'] != calendar_metadata():
        raise ValueError('入力カレンダーの版が一致しません')
    if manifest['status'] != 'prepared_complete_universe' and not audit:
        raise ValueError('部分入力は--auditを指定した資金会計監査に限定します')
    if (manifest.get('execution_price_basis') != 'adjusted_ohlc_virtual_shares'
            or manifest.get('dividend_mode') != 'embedded_in_adjusted_prices'):
        raise ValueError('主検証には調整OHLCと配当内包の明示が必要です')
    fund, fund_content, fund_source = None, None, None
    if not audit and benchmark_nav is None:
        raise ValueError('主検証には公式オルカン原本の--benchmark-navが必要です')
    if benchmark_nav is not None:
        benchmark_path = Path(benchmark_nav)
        fund_content = benchmark_path.read_bytes()
        fund_source = json.loads((benchmark_path.parent / 'source.json').read_text(encoding='utf-8'))
        if (fund_source.get('source_url') != FUND_URL
                or fund_source.get('sha256') != sha256(fund_content).hexdigest()):
            raise ValueError('オルカン原本と取得元記録のハッシュが一致しません')
        fund = load_all_country_nav_content(fund_content)
    prepared = pd.DataFrame(manifest['prepared_assets'])
    if len(prepared) != manifest['prepared_count'] or prepared.ticker.duplicated().any():
        raise ValueError('準備対象の件数または重複が不正です')
    expected = assets[assets.ticker.isin(prepared.ticker)]
    if not expected.sort_values('ticker').reset_index(drop=True).equals(prepared.sort_values('ticker').reset_index(drop=True)):
        raise ValueError('準備対象が固定母集団と一致しません')
    if not audit and set(prepared.ticker) != set(assets.ticker):
        raise ValueError('全市場の入力が揃っていません')
    def read_verified(name):
        path = (data / name).resolve()
        if not path.is_relative_to(data.resolve()):
            raise ValueError('入力ファイルの参照先が不正です')
        content = path.read_bytes()
        if sha256(content).hexdigest() != manifest['file_hashes'][name]:
            raise ValueError(f'準備済みファイルが変わっています: {name}')
        return content

    for name in manifest['file_hashes']:
        read_verified(name)
    excluded_rows = pd.DataFrame({'ticker': pd.Series(dtype=str), 'Date': pd.Series(dtype='datetime64[ns]')})
    if 'excluded_invalid_price_rows.csv' in manifest['file_hashes']:
        excluded_rows = pd.read_csv(io.BytesIO(read_verified('excluded_invalid_price_rows.csv')), parse_dates=['Date'])[['ticker', 'Date']]
        if len(excluded_rows) != manifest.get('approved_invalid_price_rows_excluded'):
            raise ValueError('除外された価格行の件数が一致しません')
    execution, prices = {}, {}
    for ticker in prepared.ticker:
        for prefix, target in [('execution', execution), ('signals', prices)]:
            target[ticker] = pd.read_csv(io.BytesIO(read_verified(f'{prefix}_{ticker}.csv')),
                    index_col='Date', parse_dates=['Date'], float_precision='round_trip')
        if not execution[ticker][prices[ticker].columns].equals(prices[ticker]):
            raise ValueError('約定価格と指標用の価格が一致しません')
    if '1306.T' not in prices:
        raise ValueError('RS参照1306.Tが必要です')
    calendar = pd.DatetimeIndex(pd.read_csv(io.BytesIO(read_verified('calendar.csv')), parse_dates=['Date']).Date)
    validate_execution_calendar(execution, prices, calendar)
    expected_days = pd.date_range('2014-01-01', '2025-12-31')
    expected_days = expected_days[trading_day_mask(expected_days)]
    observed = pd.DatetimeIndex(sorted(set().union(*(set(frame.index) for frame in prices.values()))))
    missing_all = expected_days.difference(observed)
    missing_rs = expected_days.difference(prices['1306.T'].index)
    missing_calendar = expected_days.difference(calendar)
    if len(missing_all) or len(missing_rs) or len(missing_calendar):
        issues = {'all_market': missing_all.strftime('%Y-%m-%d').tolist(),
                  'rs_reference': missing_rs.strftime('%Y-%m-%d').tolist(),
                  'calendar': missing_calendar.strftime('%Y-%m-%d').tolist()}
        output.parent.mkdir(parents=True, exist_ok=True)
        failure = output.with_name(output.name + '.preflight-failure.json')
        if failure.exists():
            raise ValueError('欠損診断の保存先が既存です。新規の出力先を指定してください')
        write_json(failure, dict(status='blocked_missing_sessions', missing_dates=issues,
                                universe_hash=universe['universe_hash'], trading_calendar=calendar_metadata()))
        raise ValueError('期待取引日の取得欠損。再準備が必要です。' + json.dumps(issues))
    if output.exists():
        raise ValueError('新規の出力先を指定してください')
    code = {str(p.relative_to(Path(__file__).resolve().parents[1])): sha256(p.read_bytes()).hexdigest()
            for p in Path(__file__).resolve().parents[1].joinpath('stock_screener').glob('*.py')}
    code['scripts/run_finite_capital_backtest.py'] = sha256(Path(__file__).read_bytes()).hexdigest()
    output.mkdir(parents=True)
    snapshot = output / 'code'
    snapshot.mkdir()
    for name in code:
        target = snapshot / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((Path(__file__).resolve().parents[1] / name).read_bytes())
    status = dict(status='running', evaluation='finite_capital', audit_only=audit,
                  universe_hash=universe['universe_hash'], input_manifest_sha256=sha256(manifest_content).hexdigest(),
                  input_hashes=manifest['file_hashes'], code_hashes=code, trading_calendar=calendar_metadata(),
                  benchmark_status='not_compared', prepared_count=len(prepared), total_universe_count=len(assets),
                  commission=commission, slippage=slippage, started_at=pd.Timestamp.now(tz='UTC').isoformat())
    status.update(data_quality_policy=manifest.get('data_quality_policy'), excluded_invalid_price_rows=len(excluded_rows))
    if len(excluded_rows):
        excluded_rows.to_csv(output / 'input_excluded_price_rows.csv', index=False)
    if fund_content is not None:
        (output / 'benchmark_original.csv').write_bytes(fund_content)
        write_json(output / 'benchmark_source.json', fund_source)
        status.update(benchmark_status='supplied', benchmark_sha256=sha256(fund_content).hexdigest())
    write_json(output / 'run.json', status)
    summary = []
    coverage = []
    try:
        strategies = STRATEGIES[:1] if audit else STRATEGIES
        policies = ('A', 'D') if audit else ('A', 'B', 'C', 'D')
        stops = (.08,) if audit else STOPS
        for period, (warmup, start, end) in PERIODS.items():
            current = {t: f.loc[warmup:end] for t, f in prices.items() if len(f.loc[warmup:end])}
            if '1306.T' not in current:
                raise ValueError('対象期間のRS参照がありません')
            for asset in prepared.itertuples():
                frame = prices[asset.ticker].loc[warmup:end]
                evaluation = frame.loc[start:end]
                coverage.append(dict(period=period, ticker=asset.ticker, asset_class=asset.asset_class,
                                     warmup_and_evaluation_rows=len(frame), evaluation_rows=len(evaluation),
                                     first_observed_date=frame.index[0] if len(frame) else None,
                                     last_observed_date=frame.index[-1] if len(frame) else None,
                                     status='observed_prices' if len(evaluation) else 'no_observed_evaluation_prices'))
            for strategy in strategies:
                flags = {}
                for ticker, frame in current.items():
                    shared = screening_signals(frame, strategy, current['1306.T'].Close)
                    columns = ['buy_signal', 'sell_signal', 'ready', 'strategy_id']
                    if 'rsi' in shared:
                        columns.append('rsi')
                    flags[ticker] = shared[columns].copy()
                for group in ('stock', 'etf'):
                    tickers = [r.ticker for r in prepared.itertuples() if r.asset_class == group and r.ticker in current]
                    if not tickers:
                        continue
                    frames = {t: execution[t].loc[warmup:end] for t in tickers}
                    decisions = {t: flags[t] for t in tickers}
                    for policy in policies:
                        for stop in stops:
                            cfg = FiniteCapitalConfig(start=start, end=end, initial_stop=stop,
                                                      commission=commission, slippage=slippage)
                            result = run_finite_capital_backtest(frames, decisions, strategy, policy,
                                                                calendar, dict.fromkeys(tickers, group), cfg)
                            target = output / period / group / strategy / f'{policy}_stop_{stop:.2f}'
                            target.mkdir(parents=True)
                            for field in ('daily', 'trades', 'positions', 'rejections', 'order_updates',
                                          'drawdown_periods', 'contributions'):
                                getattr(result, field).to_csv(target / f'{field}.csv', index=field == 'daily', float_format='%.17g')
                            result.annual_returns.to_csv(target / 'annual_returns.csv')
                            result.monthly_returns.to_csv(target / 'monthly_returns.csv')
                            if fund is not None:
                                comparison, comparison_metrics = compare_portfolio_nav(
                                    result.daily, fund, cfg.initial_cash, result.metrics['valuation_status'])
                                comparison.to_csv(target / 'all_country_comparison.csv', index=True, float_format='%.17g')
                                if audit:
                                    comparison_metrics['outperformed_on_total_return'] = None
                                    comparison_metrics['comparison_status'] = 'audit_only'
                                result.metrics.update(comparison_metrics)
                            effects, effect_metrics = excluded_row_exposure(result, excluded_rows)
                            effects.to_csv(target / 'excluded_row_effects.csv', index=False)
                            result.metrics.update(effect_metrics)
                            write_json(target / 'metrics.json', result.metrics)
                            write_json(target / 'config.json', asdict(cfg))
                            summary.append(dict(period=period, asset_class=group, strategy_id=strategy,
                                                exit_policy=policy, initial_stop=stop, **result.metrics))
                            status['completed_cases'] = len(summary)
                            status['current_case'] = dict(period=period, asset_class=group, strategy_id=strategy, exit_policy=policy, initial_stop=stop)
                            write_json(output / 'run.json', status)
                            print(f'保存 {period} {group} {strategy} {policy} {stop:.0%}', flush=True)
        pd.DataFrame(summary).drop(columns=['max_drawdown_period', 'longest_drawdown_period']).to_csv(output / 'summary.csv', index=False)
        pd.DataFrame(coverage).to_csv(output / 'period_coverage.csv', index=False)
        status.update(status='completed', completed_cases=len(summary), completed_at=pd.Timestamp.now(tz='UTC').isoformat())
        if fund is not None:
            status['benchmark_status'] = 'per_condition_comparison_saved'
        title = '有限資金の部分入力・資金会計監査' if audit else '有限資金の固定条件比較'
        report = f'# {title}\n\n全{len(summary)}条件を保存した。準備済み{len(prepared)}銘柄、固定母集団{len(assets)}銘柄。\n\n'
        report += '部分入力による動作・資金会計の監査。全市場の成績とオルカン比較の判定は保留する。\n' if audit else '公式オルカンとの同額純資産比較を条件別に保存した。\n'
        report += '\nオルカン原本を指定した場合、同額初期資金の基準価額比から純資産・CAGR差を保存する。設定前を含む2016〜2020年は全期間比較を欠損とし、古い期末価格を含む成績は最終判定を保留する。\n'
        report += f'\n入力の不整合価格行を{len(excluded_rows)}行除外した。各条件のexcluded_row_effects.csvに除外日の保有と買い見送りを記録した。行数割合と損益への影響を区分して参照する。\n'
        report += '\n設定、入力・コードのハッシュはrun.json、条件別の資金・取引・保有・注文・銘柄寄与・回復期間は各フォルダへ保存した。初期資金各1,000万円、費用込み10万円固定。period_coverage.csvには期間別の観測範囲と未観測銘柄を保存した。現在の上場一覧を過去に適用する生存者バイアスがある。\n'
        (output / 'REPORT.md').write_text(report, encoding='utf-8')
    except Exception as exc:
        status.update(status='failed', error=f'{type(exc).__name__}: {exc}', completed_cases=len(summary))
        raise
    finally:
        write_json(output / 'run.json', status)
    return status


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True)
    parser.add_argument('--universe-root', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--audit', action='store_true')
    parser.add_argument('--benchmark-nav')
    parser.add_argument('--commission', type=float, default=.001)
    parser.add_argument('--slippage', type=float, default=.0005)
    args = parser.parse_args()
    run(args.data, args.universe_root, args.output, args.audit, args.benchmark_nav,
        commission=args.commission, slippage=args.slippage)
