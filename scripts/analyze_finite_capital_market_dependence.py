#!/usr/bin/env python3
"""保存済み有限資金結果を月次・日次の市場感応度と局面別に集計する。"""
import argparse
from hashlib import sha256
import io
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock_screener.benchmark_comparison import load_all_country_nav_content

NAMES = {'tv_ema_stoch_v1': 'TradingView添付版', 'lbr_anti_310_adaptation_v1': 'Anti 3/10適応版',
         'lbr_anti_stochastic_v1': 'Anti 7/4/10版', 'legacy_ema_sma16_v1': '初期試作版',
         'minervini_screen_v1': 'ミネルヴィニ派生版', 'rsi2_screen_v1': 'RSI2', 'rsi14_screen_v1': 'RSI14'}
KEYS = ['asset_class', 'strategy_id', 'exit_policy', 'initial_stop']


def verified_bytes(path, expected):
    content = path.read_bytes()
    if sha256(content).hexdigest() != expected:
        raise ValueError('保存済み入力のハッシュが一致しません。' + str(path))
    return content


def monthly_returns(levels):
    """開始日の水準を最初の月の分母とし、月末水準から単純収益率を求める。"""
    if (levels.empty or not levels.index.is_unique or not levels.index.is_monotonic_increasing
            or not np.isfinite(levels.to_numpy()).all() or levels.le(0).any().any()):
        raise ValueError('純資産系列の値または日付が不正です')
    ends = levels.groupby(levels.index.to_period('M')).last()
    previous = ends.shift(1)
    previous.iloc[0] = levels.iloc[0]
    return ends / previous - 1


def sensitivity(strategy, benchmark):
    if not strategy.index.equals(benchmark.index) or len(strategy) < 3:
        raise ValueError('比較するリターンの日付または件数が一致しません')
    covariance = np.cov(strategy, benchmark, ddof=1)
    if not np.isfinite(covariance).all() or covariance[1, 1] <= 0 or covariance[0, 0] <= 0:
        raise ValueError('感応度を計算できない分散です')
    correlation = float(covariance[0, 1] / np.sqrt(covariance[0, 0] * covariance[1, 1]))
    return dict(observations=len(strategy), correlation=correlation,
                beta=float(covariance[0, 1] / covariance[1, 1]), r_squared=correlation ** 2)


def analyze(root, output):
    root, output = Path(root), Path(output)
    if output.exists():
        raise ValueError('新規の出力先を指定してください')
    run = root / 'primary-run-20261107-01'
    analysis = root / 'primary-analysis-01'
    data = root / 'prepared-primary-01'
    run_content = (run / 'run.json').read_bytes()
    meta = json.loads(run_content)
    verified = json.loads((analysis / 'verification.json').read_text(encoding='utf-8'))
    completion = json.loads((root / 'completion-verification.json').read_text(encoding='utf-8'))
    if meta['status'] != 'completed' or meta['completed_cases'] != 560:
        raise ValueError('完了済みの560条件が必要です')
    if sha256(run_content).hexdigest() != verified['source_run_sha256']:
        raise ValueError('元実行の記録が変わっています')
    manifest = json.loads(verified_bytes(data / 'manifest.json', meta['input_manifest_sha256']))
    source_summary = 'artifacts/' + root.name + '/primary-analysis-01/summary.csv'
    summary = pd.read_csv(io.BytesIO(verified_bytes(analysis / 'summary.csv',
                          completion['delivery_hashes'][source_summary])), float_precision='round_trip')
    cases = summary[summary.period.eq('2021_2025')].copy()
    if len(cases) != 280 or cases[KEYS].duplicated().any() or not cases.benchmark_status.eq('compared').all():
        raise ValueError('2021〜2025年の比較可能280条件が必要です')
    fund_content = verified_bytes(run / 'benchmark_original.csv', meta['benchmark_sha256'])
    fund = load_all_country_nav_content(fund_content)
    etf_content = verified_bytes(data / 'signals_1306.T.csv', manifest['file_hashes']['signals_1306.T.csv'])
    etf = pd.read_csv(io.BytesIO(etf_content), index_col='Date', parse_dates=['Date'],
                      float_precision='round_trip').Close
    base_folder = run / '2021_2025/stock/tv_ema_stoch_v1/A_stop_0.10'
    base = pd.read_csv(base_folder / 'daily.csv', index_col='date', parse_dates=['date'],
                       float_precision='round_trip')
    dates = base.index
    levels = pd.DataFrame({'all_country': fund.reindex(dates), 'topix_etf_1306': etf.reindex(dates)})
    if levels.isna().any().any():
        raise ValueError('市場系列の同日付価格が欠けています')
    market_monthly = monthly_returns(levels)
    market_daily = levels.pct_change(fill_method=None).iloc[1:]
    years = (dates[-1] - dates[0]).days / 365.2425
    market_records = []
    for market in levels:
        values = levels[market]
        market_records.append(dict(benchmark=market, total_return=float(values.iloc[-1] / values.iloc[0] - 1),
                                   cagr=float((values.iloc[-1] / values.iloc[0]) ** (1 / years) - 1),
                                   max_drawdown=float((values / values.cummax() - 1).min()),
                                   up_months=int(market_monthly[market].gt(0).sum()),
                                   down_months=int(market_monthly[market].lt(0).sum()),
                                   flat_months=int(market_monthly[market].eq(0).sum())))
    stats, regimes, monthly_tables = [], [], []
    withheld = []
    inputs = {'run.json': sha256(run_content).hexdigest(),
              'summary.csv': sha256((analysis / 'summary.csv').read_bytes()).hexdigest(),
              'manifest.json': meta['input_manifest_sha256'],
              'benchmark_original.csv': meta['benchmark_sha256'],
              'signals_1306.T.csv': manifest['file_hashes']['signals_1306.T.csv']}
    for case in cases.itertuples():
        folder = run / '2021_2025' / case.asset_class / case.strategy_id / f'{case.exit_policy}_stop_{case.initial_stop:.2f}'
        name = (folder / 'daily.csv').relative_to(run).as_posix()
        daily_content = verified_bytes(folder / 'daily.csv', verified['source_result_hashes'][name])
        inputs[name] = sha256(daily_content).hexdigest()
        daily = pd.read_csv(io.BytesIO(daily_content), index_col='date', parse_dates=['date'],
                            float_precision='round_trip')
        if not daily.index.equals(dates) or daily.nav.iloc[0] != 10_000_000:
            raise ValueError('条件ごとの評価日付または開始資金が不一致です')
        strategy_monthly = monthly_returns(daily[['nav']]).nav
        strategy_daily = daily.nav.pct_change(fill_method=None).iloc[1:]
        if len(strategy_monthly) != 60 or not np.isclose((1 + strategy_monthly).prod() - 1, case.total_return, atol=1e-12):
            raise ArithmeticError('月次の複利連結と保存済み期間損益率が一致しません')
        monthly_name = (folder / 'monthly_returns.csv').relative_to(run).as_posix()
        saved_monthly_content = verified_bytes(folder / 'monthly_returns.csv', verified['source_result_hashes'][monthly_name])
        inputs[monthly_name] = sha256(saved_monthly_content).hexdigest()
        saved_monthly = pd.read_csv(io.BytesIO(saved_monthly_content), index_col=0, parse_dates=[0], float_precision='round_trip').iloc[:, 0]
        if not np.allclose(saved_monthly, strategy_monthly, atol=1e-12):
            raise ArithmeticError('元の月次損益と再集計が一致しません')
        identity = {key:getattr(case, key) for key in KEYS}
        abnormal = strategy_daily.abs().gt(1)
        if abnormal.any():
            withheld.append(dict(identity, reason='8303_T_abnormal_mark_and_delisting_review',
                                 first_abnormal_date=str(strategy_daily.index[abnormal][0].date()),
                                 max_nav=float(daily.nav.max()), max_daily_return=float(strategy_daily.max()),
                                 min_daily_return=float(strategy_daily.min())))
            continue
        monthly_table = market_monthly.copy()
        monthly_table['strategy_return'] = strategy_monthly
        monthly_table.index.name = 'month'
        for key, value in identity.items():
            monthly_table[key] = value
        monthly_tables.append(monthly_table.reset_index())
        for market in levels:
            item = dict(identity, benchmark=market, total_return=case.total_return, cagr=case.cagr,
                        max_drawdown=case.max_drawdown)
            for frequency, strategy_values, market_values in [('monthly', strategy_monthly, market_monthly[market]),
                                                               ('daily', strategy_daily, market_daily[market])]:
                item.update({frequency+'_'+key:value for key,value in sensitivity(strategy_values, market_values).items()})
            stats.append(item)
            for regime, mask in [('up',market_monthly[market].gt(0)), ('down',market_monthly[market].lt(0)),
                                 ('flat',market_monthly[market].eq(0))]:
                own, market_values = strategy_monthly[mask], market_monthly.loc[mask, market]
                if own.empty:
                    continue
                regimes.append(dict(identity, benchmark=market, regime=regime, months=len(own),
                                    mean_strategy_return=float(own.mean()), mean_benchmark_return=float(market_values.mean()),
                                    median_strategy_return=float(own.median()), positive_month_fraction=float(own.gt(0).mean()),
                                    outperforming_month_fraction=float(own.gt(market_values).mean()),
                                    mean_excess_return=float((own-market_values).mean()),
                                    capture_ratio=float(own.mean()/market_values.mean()) if market_values.mean() else None))
    stats = pd.DataFrame(stats)
    regimes = pd.DataFrame(regimes)
    assert len(withheld) == 6 and len(stats) == 548
    output.mkdir(parents=True)
    pd.DataFrame(withheld).to_csv(output/'withheld_cases.csv',index=False,float_format='%.17g')
    stats.to_csv(output/'sensitivity.csv',index=False,float_format='%.17g')
    regimes.to_csv(output/'market_regimes.csv',index=False,float_format='%.17g')
    pd.concat(monthly_tables,ignore_index=True).to_csv(output/'case_monthly_returns.csv',index=False,float_format='%.17g')
    levels.to_csv(output/'benchmark_daily_levels.csv',index=True,float_format='%.17g')
    market_monthly.to_csv(output/'benchmark_monthly_returns.csv',index=True,float_format='%.17g')
    pd.DataFrame(market_records).to_csv(output/'benchmark_metrics.csv',index=False,float_format='%.17g')
    lines = ['# 有限資金戦略と市場環境の追加分析', '',
             '2021年1月4日〜2025年12月30日の280条件を、保存済みの純資産と価格から集計した。個別株140条件・ETF140条件。元の560条件の結果と承認済み入力をハッシュ照合し、全280条件で元の月次損益と期間損益率の複利連結を検証した。', '',
             '## 価格品質に関する保留', '',
             '8303.Tの保存価格に、2023年9月27日と2025年の出来高0・終値53,637,025,792円の行を確認した。上場廃止・再上場と株式併合・分割をまたぐ価格の連続性、指標準備、保有継承の扱いに再確認が必要。既存のOHLC範囲検査と資金会計照合では、この異常を検出できていなかった。', '',
             '全280条件の月次計算を照合したうえで、純資産の数兆円への跳ね上がりを直接確認した6条件をwithheld_cases.csvへ隔離した。以下は残る274条件・548市場組合せの参考集計。6条件の途中成績・最大DD・相関・βは保留する。残る条件も共通入力の指標計算への影響が未解決であり、価格品質の全面的な合格判定はしていない。', '',
             'オルカン超過最大ケースと出口C最大ケースでは、この純資産跳ね上がりは検出されていない。相関値は保存済み計算結果の参考値として参照する。', '',
             '公式根拠は、[JPXの2023年上場廃止決定](https://www.jpx.co.jp/news/1023/20230901-11.html)、[2025年新規上場概要](https://www.jpx.co.jp/listing/stocks/new/t13vrt000000432y-att/12SBIShinseiBank-Outlinet.pdf)、[SBIの再上場時開示](https://www.sbigroup.co.jp/news/2025/1217_15970.html)に記載されている。', '',
             '## 集計方法', '',
             '- 月次リターンを主指標、日次を参考指標とする。1月の分母は共通の評価開始日1月4日の水準で、全60か月。全系列の評価日付を一致させ、欠損は補間しない。',
             '- オルカンは公式分配金再投資基準価額。日本株市場の参照には1306.Tの配当内包調整終値を使用する。1306.TはTOPIX連動ETFであり、公式配当込みTOPIXそのものとの相違やETFの経費・乖離を含む。参照系列に新たな売買手数料は加算しない。',
             '- βは同期間のリターン共分散÷市場リターン分散。相関はリターン同士のPearson相関。R²は切片あり単回帰に相当する相関の二乗。無リスク金利控除、複数因子調整、統計的有意性検定は本集計に含まない。',
             '- 上昇・下落月は参照市場の月次リターンの正負で分類し、平均月次リターンと市場超過月の割合を比較する。キャプチャー比は各群の戦略平均÷市場平均。下落群で正の戦略利益があれば比は負になる。',
             '- 海外市場と日本市場の価格反映時刻が異なるため、同日付の日次相関には時差の影響がある。月次でも月末境界の差は残る。', '',
             '## 市場の期間成績', '',
             '| 参照系列 | 期間損益率 | CAGR | 最大DD | 上昇月 | 下落月 | 横ばい月 |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    market_names = {'all_country':'オルカン','topix_etf_1306':'TOPIX連動ETF 1306.T'}
    for market in market_records:
        lines.append(f"| {market_names[market['benchmark']]} | {market['total_return']:+.2%} | {market['cagr']:.2%} | {market['max_drawdown']:.2%} | {market['up_months']} | {market['down_months']} | {market['flat_months']} |")
    lines += ['', '## 各入口・出口の代表条件', '',
              '各入口・出口について、元の期間CAGRが最大だったストップ設定を表示する。市場相関で条件を選び直してはいない。いずれも探索内の最大値であり、独立期間で検証した結果には相当しない。', '']
    included = cases.merge(pd.DataFrame(withheld)[KEYS],on=KEYS,how='left',indicator=True)
    included = included[included._merge.eq('left_only')].drop(columns='_merge')
    best_cases = included.loc[included.groupby(['asset_class','strategy_id','exit_policy']).cagr.idxmax()]
    selected_rows = []
    for group,label in [('stock','個別株'),('etf','ETF')]:
        lines += ['### '+label, '', '| 入口 | 出口 | 損切り | CAGR | 対オルカン相関 | β | 対1306相関 | β |',
                  '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
        for case in best_cases[best_cases.asset_class.eq(group)].itertuples():
            selected = stats
            for key in KEYS:
                selected = selected[selected[key].eq(getattr(case,key))]
            selected_rows.append(selected)
            fund_stat = selected[selected.benchmark.eq('all_country')].iloc[0]
            etf_stat = selected[selected.benchmark.eq('topix_etf_1306')].iloc[0]
            lines.append(f'| {NAMES[case.strategy_id]} | {case.exit_policy} | {case.initial_stop:.0%} | {case.cagr:.2%} | {fund_stat.monthly_correlation:.3f} | {fund_stat.monthly_beta:.3f} | {etf_stat.monthly_correlation:.3f} | {etf_stat.monthly_beta:.3f} |')
    pd.concat(selected_rows,ignore_index=True).to_csv(output/'representative_cases.csv',index=False,float_format='%.17g')
    lines += ['', '## 注目ケースの局面別成績', '',
              'オルカン超過最大の個別株TradingView添付版・A・10%と、出口Cの最大CAGRの個別株RSI2・C・10%を比較する。', '',
              '| ケース | 参照市場 | 局面 | 月数 | 戦略平均月次 | 市場平均月次 | 市場を上回る月の割合 |',
              '| --- | --- | --- | ---: | ---: | ---: | ---: |']
    focus = regimes[regimes.asset_class.eq('stock') & np.isclose(regimes.initial_stop,.10)
                    & ((regimes.strategy_id.eq('tv_ema_stoch_v1')&regimes.exit_policy.eq('A'))
                       |(regimes.strategy_id.eq('rsi2_screen_v1')&regimes.exit_policy.eq('C')))]
    focus.to_csv(output/'focus_case_regimes.csv',index=False,float_format='%.17g')
    for row in focus.itertuples():
        regime_name = {'up':'上昇月','down':'下落月','flat':'横ばい月'}[row.regime]
        lines.append(f'| {NAMES[row.strategy_id]}・{row.exit_policy}・10% | {market_names[row.benchmark]} | {regime_name} | {row.months} | {row.mean_strategy_return:+.2%} | {row.mean_benchmark_return:+.2%} | {row.outperforming_month_fraction:.1%} |')
    lines += ['', '## 解釈と適用範囲', '',
              '相関とβは市場と同時に動く程度を示す。市場上昇の因果的な利益寄与、売買ルール単独の付加価値、将来の期待リターンはこの集計だけでは特定できない。R²は月次変動の線形的な対応の強さであり、累積利益の何%が市場由来かを示す数値としては扱えない。', '',
              '個別株では1306.Tを日本株市場の比較先として重視する。ETF集合は海外株・商品・債券・レバレッジ等を含むため、1306.Tとの比較は参照値として解釈する。', '',
              '2026年10月末の上場一覧による生存者・選択バイアス、仮想端数株・調整価格、承認された価格362行除外と期末含み損益を含む。高相関や高いβを確認しても、個別銘柄・業種・サイズ等の影響の分解や因果識別は未検証。2016〜2020年は今回の追加相関分析の対象に含めていない。', '',
              '各条件の期間損益率は隣接するreturn-tables-20261107-01/REPORT.md、出口Cは同フォルダのEXIT_C.mdを参照する。', '',
              '## 保存物', '',
              'sensitivity.csvは参考274条件×2参照系列の相関・β・R²。market_regimes.csvは市場局面別の平均・中央値・勝率・キャプチャー比。case_monthly_returns.csvは参考274条件の月次リターンと市場値。benchmark_daily_levels.csvとbenchmark_monthly_returns.csvは共通の参照系列。verification.jsonは入力・結果・コードのハッシュと照合件数。']
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    # 計算方法の回帰確認。開始月の欠落とβの向きの取り違えを検出する。
    synthetic = pd.DataFrame({'nav':[100.,110.,121.]},index=pd.to_datetime(['2021-01-04','2021-01-29','2021-02-26']))
    assert np.allclose(monthly_returns(synthetic).nav,[.1,.1],atol=1e-14)
    market_sample = pd.Series([-.03,.02,.01,-.01,.04])
    metric_sample = sensitivity(2*market_sample+.001,market_sample)
    assert np.isclose(metric_sample['beta'],2) and np.isclose(metric_sample['correlation'],1)
    (output/'analysis_code.py').write_bytes(Path(__file__).read_bytes())
    output_hashes = {p.name:sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()}
    result = dict(status='completed_with_price_quality_warning',period='2021_2025',checked_cases=280,reported_cases=274,withheld_cases=6,market_pairs=548,
                  monthly_observations_per_case=60,monthly_original_and_compounding_checks=280,
                  daily_observations=len(market_daily),synthetic_regression_checks=2,
                  source_inputs=inputs,output_hashes=output_hashes)
    (output/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print({key:value for key,value in result.items() if key not in ('source_inputs','output_hashes')})
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True)
    parser.add_argument('--output',required=True)
    args = parser.parse_args()
    analyze(args.root,args.output)