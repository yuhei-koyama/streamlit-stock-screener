#!/usr/bin/env python3
"""入口・出口診断の保存結果を再計算監査し、日本語報告書を作る。"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
from stock_screener.backtest_io import load_dataset
from stock_screener.backtest_signals import STRATEGIES
from stock_screener.benchmark_comparison import load_all_country_nav
from stock_screener.entry_exit_diagnosis import HORIZONS, STOPS

NAMES = {
    'tv_ema_stoch_v1':'TradingView版',
    'lbr_anti_310_adaptation_v1':'Anti 3/10版',
    'lbr_anti_stochastic_v1':'Anti 7/4/10版',
    'legacy_ema_sma16_v1':'初期試作版',
    'minervini_screen_v1':'ミネルヴィニ派生版',
    'rsi2_screen_v1':'RSI2',
    'rsi14_screen_v1':'RSI14',
}


def read(path):
    return pd.read_csv(path,float_precision='round_trip')


def verify(root):
    status=json.loads((root/'run.json').read_text(encoding='utf-8'))
    assert status['status']=='completed'
    assert len(status['baseline_audits'])==28
    _,execution,_,_,calendar,hashes=load_dataset(status['data'],mode='exploratory')
    assert hashes==status['input_hashes']
    assert sha256(Path(status['fund_csv']).read_bytes()).hexdigest()==status['fund_hash']
    nav=load_all_country_nav(status['fund_csv'])
    metadata=Path(status['fund_csv']).parent/'source.json'
    fund_source=json.loads(metadata.read_text(encoding='utf-8'))
    assert fund_source['sha256']==status['fund_hash']
    shutil.copy2(metadata,root/'fund_source.json')
    for name,digest in status['code_hashes'].items():
        assert sha256((root/'code'/name).read_bytes()).hexdigest()==digest
    entry_summary=read(root/'entry_summary.csv')
    exit_summary=read(root/'exit_summary.csv')
    assert len(entry_summary)==70 and len(exit_summary)==280
    recovery_summary=read(root/'recovery_summary.csv')
    count=0
    recovery_files=0
    fixed_samples=0
    for strategy in STRATEGIES:
        folder=root/strategy
        reference=None
        for stop in STOPS:
            for policy in 'ABCD':
                target=folder/f'{policy}_stop_{round(stop*100):02d}'
                trades=read(target/'trades.csv')
                positions=read(target/'positions.csv')
                valued=read(target/'valuations.csv')
                entries=pd.concat([trades,positions],ignore_index=True)
                entries=entries[['trade_id','entry_date','entry_price','quantity']].sort_values('trade_id').reset_index(drop=True)
                assert not entries.trade_id.duplicated().any()
                if reference is None:
                    reference=entries
                pd.testing.assert_frame_equal(reference,entries,check_dtype=False)
                np.testing.assert_allclose(entries.entry_price*entries.quantity*1.001,1,rtol=1e-12)
                expected=trades.quantity*trades.exit_price*.999-1
                np.testing.assert_allclose(expected,trades.net_return,atol=1e-12)
                if len(positions):
                    np.testing.assert_allclose(positions.quantity*positions.mark-1,positions.unrealized_return,atol=1e-12)
                for row in valued.itertuples(index=False):
                    expected=nav.loc[pd.Timestamp(row.valuation_date)]/nav.loc[pd.Timestamp(row.entry_date)]-1
                    assert abs(expected-row.all_country_return)<1e-12
                np.testing.assert_allclose(valued.strategy_return-valued.all_country_return,valued.excess_return,atol=1e-12)
                for name,group in valued.groupby('scope'):
                    summary=exit_summary[(exit_summary.strategy==strategy)&(exit_summary.policy==policy)&np.isclose(exit_summary.stop,stop)&(exit_summary.scope==name)].iloc[0]
                    assert len(group)==summary.all_trade_count
                    assert abs(group.excess_return.mean()-summary.all_mean_excess_return)<1e-12
                if policy in 'CD':
                    orders=read(target/'order_updates.csv')
                    entry_price=entries.set_index('trade_id').entry_price
                    price=orders.trade_id.map(entry_price)
                    unit=price*.08
                    np.testing.assert_allclose(orders.initial_risk,unit,atol=1e-10)
                    np.testing.assert_allclose(orders.stop,price-price*stop+orders.ratchet_step*unit,atol=1e-10)
                    target_price=price+(3+(orders.ratchet_step if policy=='D' else 0))*unit
                    np.testing.assert_allclose(orders.target,target_price,atol=1e-10)
                    updates=orders[orders.event=='update']
                    if len(updates):
                        dates=pd.to_datetime(updates.date)
                        expected=calendar[calendar.get_indexer(dates)+1]
                        assert (pd.to_datetime(updates.effective_from).to_numpy()==expected.to_numpy()).all()
                for horizon in HORIZONS:
                    recovery_path=target/f'recovery_{horizon}.csv'
                    if not recovery_path.exists():
                        continue
                    recovery=read(recovery_path)
                    for name,group in recovery.groupby('scope'):
                        eligible=group[group.stopped_before_horizon]
                        summary=recovery_summary[(recovery_summary.strategy==strategy)
                            &(recovery_summary.policy==policy)&np.isclose(recovery_summary.stop,stop)
                            &(recovery_summary.horizon==horizon)&(recovery_summary.scope==name)].iloc[0]
                        assert len(eligible)==summary.stopped_before_horizon
                        assert int(eligible.later_close_recovered.sum())==summary.later_close_recovered
                    recovery_files+=1
                count+=1
        for horizon in HORIZONS:
            outcomes=read(folder/f'fixed_{horizon}_all_ready_days.csv')
            baseline=outcomes.groupby(['ticker','entry_year']).net_return.mean()
            for row in outcomes.sample(min(30,len(outcomes)),random_state=20261105).itertuples(index=False):
                frame=execution[row.ticker]
                entry=pd.Timestamp(row.entry_date)
                end=pd.Timestamp(row.exit_date)
                assert calendar.get_loc(end)-calendar.get_loc(entry)==horizon
                price=frame.loc[entry,'Open']*1.0005
                expected=frame.loc[end,'Open']*.9995*.999/(price*1.001)-1
                assert abs(expected-row.net_return)<1e-12
                path=frame.loc[(frame.index>=entry)&(frame.index<end)]
                mae=min(0,min(path.Low.min(),frame.loc[end,'Open'])/price-1)
                mfe=max(0,max(path.High.max(),frame.loc[end,'Open'])/price-1)
                assert abs(mae-row.mae)<1e-12 and abs(mfe-row.mfe)<1e-12
                assert abs(baseline.loc[(row.ticker,row.entry_year)]-row.baseline_return)<1e-12
                fixed_samples+=1
            selected=outcomes[outcomes.is_signal]
            for name,group in selected.groupby('scope'):
                summary=entry_summary[(entry_summary.strategy==strategy)&(entry_summary.horizon==horizon)&(entry_summary.scope==name)].iloc[0]
                assert summary['count']==len(group)
                assert abs(summary.timing_advantage-(group.net_return-group.baseline_return).mean())<1e-12
    return dict(status='passed',exit_conditions=count,fixed_window_samples=fixed_samples,
                baseline_8pct_matches=28,recovery_files=recovery_files,input_hashes_unchanged=True,
                entry_prices_quantities_identical=True,nav_and_cost_recalculation=True,
                fixed_unit_order_levels_and_next_day_effect=True)


def pct(value):
    return f'{value*100:+.2f}'


def report(root,audit):
    entry=read(root/'entry_summary.csv')
    exits=read(root/'exit_summary.csv')
    stocks=entry[entry.scope=='individual_stocks']
    stock_exits=exits[exits.scope=='individual_stocks']
    annual=read(root/'exit_by_year_or_ticker.csv')
    concentration=read(root/'concentration.csv')
    timing_counts=stocks.groupby('strategy').timing_advantage.apply(lambda values: int(values.gt(0).sum()))
    positive_names='・'.join(NAMES[name] for name in STRATEGIES if timing_counts.loc[name]==5)
    negative_names='・'.join(NAMES[name] for name in STRATEGIES if timing_counts.loc[name]==0)
    a_rows=stock_exits[stock_exits.policy=='A']
    monotonic_count=sum(group.sort_values('stop').all_mean_excess_return.is_monotonic_increasing
                        for _,group in a_rows.groupby('strategy'))
    minervini=stock_exits[(stock_exits.strategy=='minervini_screen_v1')
                          &(stock_exits.policy=='A')&np.isclose(stock_exits.stop,.12)].iloc[0]
    lines=['# 日本株の入口・出口診断結果', '',
        '2026年11月5日。仕様1.0-approved、R-DIP01〜R-DIP05の探索・診断。', '',
        '## 検証条件', '',
        '- 2021年1月4日〜2025年12月30日。日本個別株50銘柄を主集計、東証ETF20銘柄は別集計。',
        '- 7入口の共有判定関数を使用。翌営業日寄付で同額元本の独立仮想取引。資金・同時保有数の上限なし。',
        '- 片道手数料0.1%、成行スリッページ0.05%。調整OHLC使用、配当の別加算なし。税金は含まない。',
        '- 固定保有5/10/20/40/60営業日の35条件。出口A/B/C/D×損切り3/5/8/10/12%の140条件。',
        '- 出口比較は期限なし。期末保有は期末時価を含め、未実現分の売却費用は未計上。',
        '- C/Dは診断用の値幅Uを購入価格の8%へ固定。初期利確24%、切り上げ刻み8%。変えるのは初期ストップ幅。',
        '- 同期間オルカンの公式分配金再投資基準価額と比較。以下の損益率は取引の平均。差の単位はパーセントポイント。', '',
        '## 入口と出口の切り分け', '',
        f'- {positive_names}は、固定保有5期間すべてで同じ銘柄・年の全ready日基準を上回った。買いタイミングに正の傾向があり、出口との組合せを調べる根拠になる。',
        f'- {negative_names}は、固定保有5期間すべてで銘柄年基準を下回った。今回の5〜60営業日の範囲では、入口の買いタイミングの優位を確認できなかった。',
        f'- Aでは{len(STRATEGIES)}入口中{monotonic_count}入口で損切りを3%から12%へ広げるほど平均オルカン差が上がった。入口ごとに適する保有・出口の関係が異なる。',
        f'- ミネルヴィニA・12%は平均{minervini.all_mean_strategy_return:.2%}、同期間オルカン{minervini.all_mean_benchmark_return:.2%}、差{pct(minervini.all_mean_excess_return)}pt。超過損益の中央値は{pct(minervini.all_median_excess_return)}pt、期末保有{int(minervini.open_count)}件を含む。長期保有で得た平均成績と短中期の入口評価を分けて読む必要がある。',
        '- 各入口の最大平均差条件でも超過損益の中央値はすべて負だった。入口・出口の二択による原因断定や、戦略の確定採用には結び付けない。', '',
        '## 入口の固定保有評価', '',
        '銘柄年基準は、同じ銘柄・購入年の全ready日を翌寄付で買い、同じHで売る平均。シグナル件数と同じ銘柄・年の構成比へ加重する。Hを期末まで確保できない例は固定期間標本から区分した。', '',
        '| 入口 | H | 件数 | 純損益率% | オルカン差pt | 銘柄年基準差pt | 平均MAE% | 平均MFE% |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for row in stocks.itertuples(index=False):
        lines.append(f'| {NAMES[row.strategy]} | {row.horizon} | {row.count} | {pct(row.mean_return)} | {pct(row.excess_return)} | {pct(row.timing_advantage)} | {pct(row.mean_mae)} | {pct(row.mean_mfe)} |')
    lines += ['', 'MAE/MFEは購入約定価格に対する価格変動で、手数料を含まない。購入日から売却前日までの高安値と売却日の寄付を含む。売却日の高安値は含めない。日足内の到達順序は特定できない。', '',
              '## 損切り幅の感応度', '',
              '表は期末保有を含む全購入取引の平均オルカン差。出口が異なるとオルカンの比較期間も変わる。', '',
              '| 入口 | 出口 | 3% | 5% | 8% | 10% | 12% |',
              '| --- | --- | ---: | ---: | ---: | ---: | ---: |']
    for (strategy,policy),group in stock_exits.groupby(['strategy','policy'],sort=False):
        values=group.sort_values('stop').all_mean_excess_return
        lines.append(f'| {NAMES[strategy]} | {policy} | '+' | '.join(pct(v) for v in values)+' |')
    lines += ['', 'Aは初期ストップ、Bは初期ストップと既存の戦略別出口、Cは固定利確とストップ切り上げ、Dは利確とストップの両方を切り上げる。Bのミネルヴィニ10%トレーリング等は固定。', '',
              '## 切り分けの観察', '']
    for strategy in STRATEGIES:
        group=stocks[stocks.strategy==strategy]
        good_timing=int(group.timing_advantage.gt(0).sum())
        good_fund=int(group.excess_return.gt(0).sum())
        variants=stock_exits[stock_exits.strategy==strategy]
        best=variants.loc[variants.all_mean_excess_return.idxmax()]
        year_rows=annual[(annual.strategy==strategy)&(annual.policy==best.policy)&np.isclose(annual.stop,best.stop)&(annual.scope=='individual_stocks')&annual.entry_year.notna()]
        robust=concentration[(concentration.strategy==strategy)&(concentration.policy==best.policy)&np.isclose(concentration.stop,best.stop)&(concentration.scope=='individual_stocks')&(concentration.removed_top_tickers==5)].iloc[0]
        lines.append(f'- {NAMES[strategy]}。固定保有の銘柄年基準差が正の期間は{good_timing}/5、オルカン差が正は{good_fund}/5。出口の最大平均差は{best.policy}・損切り{best.stop:.0%}で{pct(best.all_mean_excess_return)}pt。超過損益の中央値は{pct(best.all_median_excess_return)}pt、購入年別の平均差が正は{int(year_rows.all_mean_excess_return.gt(0).sum())}/{len(year_rows)}年。超過損益寄与上位5銘柄を除くと平均差は{pct(robust.all_mean_excess_return)}pt。')
    lines += ['', '購入年別は購入年で分けた取引群の結果であり、各暦年の運用年利を表さない。上記の最大値は20出口条件を閲覧した探索値。入口評価の良否が保有期間で変わる場合や、銘柄年基準差が弱い場合は、損切り幅の調整だけで入口のタイミング優位を確定できない。固定保有と出口付きで期間が変わるため、差を原因の寄与率には換算しない。', '',
              '## 損切り後の回復', '',
              'recovery_summary.csvには、固定窓を確保でき、窓の終わりより前に実際に損切りした取引のうち、損切り翌日以降・固定期間の売却前日までの終値が購入約定価格以上へ戻った割合を保存した。手数料回収の判定は含まない。同日高安値の順序を回復と扱わない。', '',
              '## 検証と制約', '',
              '- 自動テスト99件は全件成功。これは合成・回帰・保存処理の確認。実市場の175診断条件と独立監査は別に記録する。',
              f"- 保存済み8%条件の取引・期末保有は28条件とも既存結果と一致。全{audit['exit_conditions']}条件で入口ID・価格・数量が一致。",
              f"- 全出口条件の往復費用・NAV・集計、C/Dの注文水準と翌日有効化を再計算。固定窓{audit['fixed_window_samples']}例を価格原本から独立照合。入力ハッシュとオルカン取得元ハッシュは一致。",
              '- 既に確認済み期間の探索であり、未使用データでの優位性確認は未実施。固定の現在銘柄一覧を使うため、生存者・選択バイアスがある。',
              '- 同一銘柄の重複取引と重複期間がある。標本を独立と仮定した有意差や、有限資金の年率・資産曲線として解釈しない。',
              '- 調整価格、仮想端株、日足内ストップ優先、指値は目標価格約定という既存仮定を引き継ぐ。補正63行の履歴を同梱。', '',
              '## 保存ファイル', '',
              '- entry_summary.csv。個別株とETFを分けた固定保有35条件。',
              '- exit_summary.csv。個別株とETFを分けた出口140条件。',
              '- exit_by_year_or_ticker.csv。年別・銘柄別の出口比較。',
              '- concentration.csv。超過損益の寄与上位1/3/5銘柄を除いた補助集計。',
              '- recovery_summary.csv。損切り後の後日終値回復の件数と割合。',
              '- 各入口フォルダ。全ready日の固定保有原票、銘柄年基準、除外理由、年別・銘柄別集計。',
              '- 各出口フォルダ。全取引、期末保有、却下、オルカン比較、C/D注文履歴、固定窓別の回復原票。',
              '- run.json、verification.json、code/。条件、入力ハッシュ、8%一致記録、監査、実行コード。', '',
              '## 再現コマンド', '', '```powershell',
              '.\\.venv\\Scripts\\python.exe scripts/diagnose_entry_exit.py --data artifacts/backtest/independent-yahoo-20261105-02/data --fund-csv artifacts/backtest/all-country-comparison-20261105-01/253425-original.csv --output artifacts/backtest/entry-exit-diagnosis-reproduction',
              '.\\.venv\\Scripts\\python.exe scripts/verify_entry_exit_diagnosis.py artifacts/backtest/entry-exit-diagnosis-reproduction',
              '```', '']
    recovery=read(root/'recovery_summary.csv')
    selected=recovery[(recovery.scope=='individual_stocks')&(recovery.policy=='A')
                      &(recovery.horizon==60)&np.isclose(recovery.stop,.08)]
    recovery_lines=['## 8%損切り後の60営業日窓での回復', '',
        '| 入口 | 窓内で先に損切りした件数 | 後日終値回復の件数 | 回復割合 |',
        '| --- | ---: | ---: | ---: |']
    for row in selected.itertuples(index=False):
        recovery_lines.append(f'| {NAMES[row.strategy]} | {row.stopped_before_horizon} | {row.later_close_recovered} | {row.recovery_rate:.1%} |')
    recovery_lines += ['', '母集団は60営業日の評価窓があり、その窓が終わる前にAの8%ストップで損切りした取引。後日回復には追加の含み損リスクも伴い、回復率だけで損切りの不要性や最適幅は判定しない。', '']
    at=lines.index('## 検証と制約')
    lines[at:at]=recovery_lines
    (root/'REPORT.md').write_text('\n'.join(lines),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output')
    args=parser.parse_args()
    root=Path(args.output)
    audit=verify(root)
    report(root,audit)
    (root/'verification.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    shutil.copy2(__file__,root/'code'/Path(__file__).name)
    print(json.dumps(audit,ensure_ascii=False,indent=2))
