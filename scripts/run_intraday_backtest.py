#!/usr/bin/env python3
"""承認済み5分足入力を検証し、監査用の限定条件または全280条件を保存する。"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
from time import perf_counter
import platform
import statistics
import sys

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO))
from stock_screener.backtest_signals import RSI2, STRATEGIES
from stock_screener.benchmark_comparison import compare_portfolio_nav, load_all_country_nav_content
from stock_screener.exploratory_data import digest
from stock_screener.intraday import IntradayConfig, STOPS, intraday_signals, regular_bar_index, run_intraday_backtest
from stock_screener.linda_versions import TV
from stock_screener.market_research import write_json


def load_pilot(root):
    root=Path(root)
    source=root/'collection-02'
    state=json.loads((source/'collection.json').read_text(encoding='utf-8'))
    if state['status']!='collected_pending_quality_audit':
        raise ValueError('固定原本の取得完了が必要です')
    for name,expected in state['hashes'].items():
        if digest(source/name)!=expected:
            raise ValueError(f'原本のハッシュが不一致です {name}')
    plan=json.loads((root/'proposal.json').read_text(encoding='utf-8'))
    settings=plan['adopted_conditions']
    tickers=[x['ticker'] for x in settings['tradable_stocks']]
    if set(state['tickers'])!=set(tickers)|{'1306.T'}:
        raise ValueError('固定対象が一致しません')
    adoption=json.loads((root/'input-adoption-approval.json').read_text(encoding='utf-8'))
    implementation=json.loads((root/'implementation-approval.json').read_text(encoding='utf-8'))
    if (adoption['answers']['input_adoption']!='原本固定と品質上の注記を承認する（推奨）'
            or adoption['answers']['morning_close']!='9:25足終値による近似を承認する（推奨）'
            or implementation['status']!='approved'):
        raise ValueError('入力採用・朝終端価格・実装の承認記録が必要です')
    calendar_path=root/'quality-audit-02/calendar.json'
    quality=json.loads((root/'quality-audit-02/verification.json').read_text(encoding='utf-8'))
    if digest(calendar_path)!=quality['output_hashes']['calendar.json']:
        raise ValueError('カレンダー保存物が一致しません')
    calendar=json.loads(calendar_path.read_text(encoding='utf-8'))
    if calendar['policy']!='jpx_cash_2026_intraday_pilot_v1' or calendar['source_sha256']!=digest(source/'raw/calendar-attempt-1.bin'):
        raise ValueError('営業日規則の版または公式原本が一致しません')
    start=pd.Timestamp(state['start_inclusive']).tz_localize(None)
    end=pd.Timestamp(state['end_exclusive']).tz_localize(None)
    days=pd.date_range(start,end,inclusive='left')
    days=days[(days.dayofweek<5)&~days.isin(pd.to_datetime(calendar['holidays']))]
    grid=regular_bar_index(days)
    prices,daily,auxiliary,splits={},{},{},{}
    for ticker in state['tickers']:
        asset=state['assets'][ticker]
        frame=pd.read_csv(source/asset['5m']['file'],index_col='Timestamp',parse_dates=True,float_precision='round_trip')
        prices[ticker]=frame.loc[frame.index.isin(grid)].copy()
        auxiliary[ticker]=frame.loc[frame.index.strftime('%H:%M')=='11:30'].copy()
        day_prices=pd.read_csv(source/asset['1d']['file'],index_col='Timestamp',parse_dates=True,float_precision='round_trip')
        day_prices.index=day_prices.index.tz_localize(None).normalize()
        daily[ticker]=day_prices
        events=json.loads((source/asset['1d']['events_file']).read_text(encoding='utf-8'))
        splits[ticker]=[pd.Timestamp(event['date'],unit='s',tz='UTC').tz_convert('Asia/Tokyo').tz_localize(None).normalize()
                        for event in events.get('splits',{}).values()]
    nav=load_all_country_nav_content((source/'all_country_original.csv').read_bytes())
    evaluation=days[settings['warmup_sessions']:]
    if evaluation.empty or len(evaluation.difference(nav.index)):
        raise ValueError('評価期間またはオルカンの営業日が不足しています')
    provenance=dict(source_collection_sha256=digest(source/'collection.json'),input_hashes=state['hashes'],
        calendar_sha256=digest(calendar_path),fixed_tickers=tickers,reference_ticker='1306.T',
        warmup_sessions=settings['warmup_sessions'],sessions=days.strftime('%Y-%m-%d').tolist(),
        evaluation_sessions=evaluation.strftime('%Y-%m-%d').tolist(),
        settings=settings,benchmark_source=state['benchmark'])
    return prices,daily,auxiliary,splits,days,nav,provenance


def run(data,output,audit=False,timing_repeats=3):
    data,output=Path(data),Path(output)
    if output.exists() or timing_repeats<1:
        raise ValueError('新規の出力先と正の測定回数が必要です')
    prices,daily,auxiliary,splits,days,nav,provenance=load_pilot(data)
    code_paths=[*REPO.joinpath('stock_screener').glob('*.py'),Path(__file__)]
    code={p.relative_to(REPO).as_posix():digest(p) for p in code_paths}
    output.mkdir(parents=True)
    for path in code_paths:
        target=output/'code'/path.relative_to(REPO)
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(path.read_bytes())
    status=dict(status='running',audit_only=audit,completed_cases=0,input=provenance,code_hashes=code)
    write_json(output/'run.json',status)
    timing,flags={},{}
    try:
        for strategy in STRATEGIES:
            measurements=[]
            for _ in range(timing_repeats):
                started=perf_counter()
                result=intraday_signals(prices,strategy,days,splits)
                measurements.append(perf_counter()-started)
            flags[strategy]=result
            latest_measurements=[]
            cutoff=regular_bar_index(days)[-1]+pd.Timedelta(5,unit='min')
            for _ in range(timing_repeats):
                started=perf_counter()
                latest=intraday_signals(prices,strategy,days,splits,decision_time=cutoff,latest_only=True)
                latest_measurements.append(perf_counter()-started)
            for ticker in prices:
                pd.testing.assert_frame_equal(result[ticker].tail(1),latest[ticker])
            timing[strategy]=dict(seconds=measurements,median_seconds=statistics.median(measurements),
                                  min_seconds=min(measurements),max_seconds=max(measurements),
                                  latest_seconds=latest_measurements,
                                  latest_median_seconds=statistics.median(latest_measurements))
            print(f'指標実測 {strategy} 全履歴{statistics.median(measurements):.3f}秒・最新足{statistics.median(latest_measurements):.3f}秒',flush=True)
        total_seconds=sum(x['median_seconds'] for x in timing.values())
        latest_seconds=sum(x['latest_median_seconds'] for x in timing.values())
        source_state=json.loads((data/'collection-02/collection.json').read_text(encoding='utf-8'))
        request_times=pd.to_datetime([r['requested_at'] for r in source_state['requests'] if 'finance.yahoo.com' in r['url']],utc=True)
        seconds_per_request=float(pd.Series((request_times[1:]-request_times[:-1]).total_seconds()).mean())
        estimates=[dict(tickers=count,seven_logic_compute_seconds=total_seconds*count/len(prices),
                        seven_logic_latest_seconds=latest_seconds*count/len(prices),
                        serial_one_request_per_ticker_seconds=seconds_per_request*count)
                   for count in (21,50,100,4000)]
        performance=dict(measured_tickers=len(prices),reference_included=True,rows=sum(len(f) for f in prices.values()),
            timing_repeats=timing_repeats,seven_logic_median_seconds=total_seconds,by_strategy=timing,
            seven_logic_latest_median_seconds=latest_seconds,latest_matches_full_history=True,
            computation='保存済み全履歴の指標計算と欠損・分割の区間処理。ネットワークと画面表示を含まない。',
            latest_computation='最後の確定足が属する有効区間だけを共有計算式で評価。全履歴の最新結果と一致を照合。',
            extrapolations=estimates,extrapolation='21銘柄の実測値を銘柄数に比例させた推定。50・100・4000の実測値は今後の課題。',
            serial_request_interval_seconds=seconds_per_request,python=platform.python_version(),platform=platform.platform())
        write_json(output/'performance.json',performance)
        summaries,checks=[],[]
        tickers=provenance['fixed_tickers']
        execution={t:prices[t] for t in tickers}
        daily_prices={t:daily[t] for t in tickers}
        event_prices={t:auxiliary[t] for t in tickers}
        evaluation=pd.DatetimeIndex(provenance['evaluation_sessions'])
        cases=[(strategy,policy,stop) for strategy in STRATEGIES for policy in ('A','B','C','D') for stop in STOPS]
        if audit:
            cases=[(TV,'A',.005),(TV,'D',.005),(RSI2,'A',.005),(RSI2,'C',.005)]
        for window in ('full_session','morning_0900_0930'):
            for strategy,policy,stop in cases:
                cfg=IntradayConfig(start=str(evaluation[0].date()),end=str(evaluation[-1].date()),window=window,
                    initial_cash=provenance['settings']['initial_cash'],
                    purchase_budget=provenance['settings']['purchase_budget_including_fee'],
                    commission=provenance['settings']['commission_per_side'],
                    slippage=provenance['settings']['market_slippage_per_side'],initial_stop=stop,
                    ratchet_reference_stop=provenance['settings']['ratchet_reference_stop'],
                    minervini_trailing=provenance['settings']['minervini_B_trailing_rate'])
                started=perf_counter()
                result=run_intraday_backtest(execution,{t:flags[strategy][t] for t in tickers},
                    strategy,policy,days,daily_prices,cfg,event_prices)
                elapsed=perf_counter()-started
                target=output/window/strategy/f'{policy}_stop_{stop:.3f}'
                target.mkdir(parents=True)
                for name in ('bars','daily','trades','positions','rejections','order_updates','quality_events','contributions','drawdown_periods'):
                    getattr(result,name).to_csv(target/f'{name}.csv',index=name in ('bars','daily'),float_format='%.17g')
                comparison,benchmark_metrics=compare_portfolio_nav(result.daily,nav,cfg.initial_cash,
                    'current_prices' if result.metrics['rank_eligible'] else 'reference_intraday_path')
                comparison.to_csv(target/'all_country_comparison.csv',float_format='%.17g')
                topix_comparison,topix_metrics=compare_portfolio_nav(
                    result.daily,daily['1306.T']['Adj Close'],cfg.initial_cash,
                    'current_prices' if result.metrics['rank_eligible'] else 'reference_intraday_path')
                topix_comparison.rename(columns={'all_country_nav':'topix_etf_nav'}).to_csv(
                    target/'topix_etf_comparison.csv',float_format='%.17g')
                if audit or not result.metrics['rank_eligible']:
                    benchmark_metrics.update(outperformed_on_total_return=None,
                        benchmark_status='audit_only' if audit else 'reference_intraday_price_paths')
                    topix_metrics.update(outperformed_on_total_return=None,
                        benchmark_status='audit_only' if audit else 'reference_intraday_price_paths')
                result.metrics.update(benchmark_metrics)
                result.metrics.update({f'topix_{key}':value for key,value in topix_metrics.items()})
                write_json(target/'config.json',asdict(cfg))
                write_json(target/'metrics.json',result.metrics)
                if not result.trades.empty:
                    trade_days=result.trades.entry_time.dt.tz_localize(None).dt.normalize()
                    expected_nav=cfg.initial_cash+result.trades.net_pnl.groupby(trade_days).sum().reindex(result.daily.index,fill_value=0).cumsum()
                else:
                    expected_nav=pd.Series(cfg.initial_cash,index=result.daily.index)
                all_flat=bool(result.daily.positions.eq(0).all())
                passed=bool(np.allclose(result.daily.nav,expected_nav,rtol=1e-10,atol=.01)) if all_flat else True
                if not passed:
                    raise ArithmeticError('日次の資金会計が不一致です')
                checks.append(dict(window=window,strategy_id=strategy,exit_policy=policy,initial_stop=stop,
                    accounting_passed=passed,all_sessions_flat=all_flat,
                    run_status=result.metrics['run_status'],quality_status=result.metrics['quality_status'],
                    execution_seconds=elapsed))
                summaries.append(dict(window=window,strategy_id=strategy,exit_policy=policy,initial_stop=stop,**result.metrics))
                status.update(completed_cases=len(summaries),current_case=checks[-1])
                write_json(output/'run.json',status)
                print(f'動作検算保存 {window} {strategy} {policy} {stop:.1%}',flush=True)
        pd.DataFrame(summaries).to_csv(output/'summary.csv',index=False,float_format='%.17g')
        pd.DataFrame(checks).to_csv(output/'accounting_checks.csv',index=False,float_format='%.17g')
        title='分足処理の限定条件による動作検算' if audit else '5分足デイトレの全280条件'
        lines=[f'# {title}','','保存済み20個別株とRS参照1306.Tを使い、7入口の指標計算時間と価格・注文・資金処理を確認した。', '',
            f'保存した実データ条件は{len(summaries)}件。監査用の限定条件では成績の順位付けを保留する。通常の本実行は280条件を対象にする。','',
            '## 計算時間の実測','',f'21銘柄の7ロジック全履歴計算は、各{timing_repeats}回の中央値を合計して{total_seconds:.3f}秒。最新確定足のみの判定は{latest_seconds:.3f}秒。ネットワーク取得と画面表示は含めない。', '',
            '最新足の判定は、欠損・分割で区切られた最後の有効区間を計算し、全履歴の最新結果と一致を確認した。将来の連続した長い履歴では区間長が増えるため、最新足判定時間は今回のデータ構成にも依存する。', '',
            '| 銘柄数 | 7ロジック全履歴 | 最新確定足の7ロジック | 逐次取得1要求/銘柄 |','| ---: | ---: | ---: | ---: |']
        for row in estimates:
            label='実測' if row['tickers']==21 else '比例推定'
            lines.append(f"| {row['tickers']:,} | {row['seven_logic_compute_seconds']:.2f}秒（{label}） | {row['seven_logic_latest_seconds']:.2f}秒（{label}） | {row['serial_one_request_per_ticker_seconds']:.1f}秒 |")
        lines+=['','## 約定と品質の前提','',
            '確定足から次足始値へ注文し、損切り・利確を終了時刻の手仕舞いより先に処理する。ストップと利確の同一足両到達はストップ優先。足内決済時刻は足末で近似する。', '',
            '足の正の出来高で約定可能性を確認する仮定を含む。同じ始値で約定できなかった買い注文の購入枠は足末まで予約し、その始値の別注文へ転用することを防いだ。', '',
            '前引け11:30の補助行は価格出口の確認に使用し、終了確認時刻を11:35とする近似。通常指標の本数は通常区間の5分足で数える。価格原本の欠損・分割境界では指標を再準備する。', '',
            '通常版の手仕舞いは同日日足終値、朝限定版は9:25足終値の近似。終端価格が欠けた保有は未決済のまま記録し、その時点で条件の評価を停止する。保有中に欠損等がある成績は参考値として保存する。', '',
            '## 保存内容','',
            'オルカンと1306.Tの同額保有を日次比較する。基準は評価初日終値で、戦略には初日の場中損益も含まれる。1306.Tは保存済みYahoo調整終値を使用し、分配・分割調整を配信値に依存する。比較対象の購入・売却費用と投資家税は0とする。', '',
            'performance.jsonに測定環境・反復値・比例推定、accounting_checks.csvに日次会計と手仕舞い状態、run.jsonに入力・コードのハッシュ、各条件フォルダに全取引・足別資産・日次資産・注文更新・欠損・寄与と設定を保存した。']
        (output/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
        status.update(status='completed',completed_cases=len(summaries),accounting_passed_cases=len(checks))
    except Exception as exc:
        status.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        write_json(output/'run.json',status)
    return status


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--audit',action='store_true')
    parser.add_argument('--timing-repeats',type=int,default=3)
    args=parser.parse_args()
    run(args.data,args.output,args.audit,args.timing_repeats)
