"""保存済みの独立仮想取引とオルカンの同日付基準価額を比較する。"""
import json
from pathlib import Path
import platform
import unicodedata

import numpy as np
import pandas as pd

from .backtest_signals import STRATEGIES
from .exploratory_data import digest


NAV_COLUMN = '基準価額（分配金再投資）(円)'
COMPARISON_METHOD = 'all_country_same_dates'
FUND_URL = 'https://www.am.mufg.jp/fund_file/setteirai/253425.csv'


def load_all_country_nav(path):
    """公式CSVのファンド名・列・日付・正値を確認して再投資系列を返す。"""
    title = Path(path).read_text(encoding='cp932').splitlines()[0]
    normalized = unicodedata.normalize('NFKC', title)
    if ('emaxis slim' not in normalized.lower() or '全世界株式' not in normalized
            or 'オール・カントリー' not in normalized):
        raise ValueError('Unexpected fund name')
    frame = pd.read_csv(path, encoding='cp932', skiprows=1, float_precision='round_trip')
    if not {'基準日', NAV_COLUMN}.issubset(frame) or frame.empty:
        raise ValueError('Official reinvested NAV columns required')
    dates = pd.DatetimeIndex(pd.to_datetime(frame['基準日'], format='%Y/%m/%d', errors='raise'))
    if dates.hasnans or not dates.is_unique or not dates.is_monotonic_increasing:
        raise ValueError('NAV dates must be unique and ascending')
    values = pd.to_numeric(frame[NAV_COLUMN], errors='raise').to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError('NAV must be finite and positive')
    return pd.Series(values, index=dates, name='all_country_nav')


def compare_trades(trades, nav):
    """同額元本の各取引へ基準価額の期間収益率と差を付加する。"""
    required = {'trade_id', 'ticker', 'asset_class', 'entry_date', 'exit_date', 'entry_cost', 'net_return'}
    if not required.issubset(trades):
        raise ValueError('Trade fields missing')
    out = trades.copy()
    if out.trade_id.isna().any() or not out.trade_id.is_unique:
        raise ValueError('Trade ids must be unique')
    for field in ('entry_date', 'exit_date'):
        out[field] = pd.to_datetime(out[field], errors='raise')
        if out[field].isna().any():
            raise ValueError('Trade date missing')
    if out.exit_date.lt(out.entry_date).any():
        raise ValueError('Exit date must follow entry date')
    if not np.isfinite(out.net_return.to_numpy(dtype=float)).all():
        raise ValueError('Trade returns must be finite')
    if not np.isclose(out.entry_cost.to_numpy(dtype=float), 1.0, rtol=0, atol=1e-12).all():
        raise ValueError('Equal normalized entry cost 1 required')
    required_dates = pd.DatetimeIndex(out.entry_date).union(pd.DatetimeIndex(out.exit_date))
    missing = required_dates.difference(nav.index)
    if len(missing):
        raise ValueError(f'Missing exact NAV dates: {missing.strftime("%Y-%m-%d").tolist()}')
    out['all_country_entry_nav'] = nav.reindex(pd.DatetimeIndex(out.entry_date)).to_numpy()
    out['all_country_exit_nav'] = nav.reindex(pd.DatetimeIndex(out.exit_date)).to_numpy()
    out['all_country_return'] = out.all_country_exit_nav / out.all_country_entry_nav - 1
    out['excess_return'] = out.net_return - out.all_country_return
    out['strategy_terminal_value'] = 1 + out.net_return
    out['all_country_terminal_value'] = 1 + out.all_country_return
    out['holding_calendar_days'] = (out.exit_date - out.entry_date).dt.days
    out['entry_year'] = out.entry_date.dt.year
    return out


def comparison_statistics(trades):
    """取引等加重の損益率と超過差を集計する。"""
    count = len(trades)
    return dict(
        trade_count=count,
        mean_net_return=float(trades.net_return.mean()) if count else None,
        mean_all_country_return=float(trades.all_country_return.mean()) if count else None,
        mean_excess_return=float(trades.excess_return.mean()) if count else None,
        median_excess_return=float(trades.excess_return.median()) if count else None,
        outperform_rate=float(trades.excess_return.gt(0).mean()) if count else None,
        same_day_trades=int(trades.holding_calendar_days.eq(0).sum()),
        average_calendar_days=float(trades.holding_calendar_days.mean()) if count else None,
    )


def grouped_comparison(trades, field):
    records = [{field: key, **comparison_statistics(group)}
               for key, group in trades.groupby(field, sort=True)]
    return pd.DataFrame(records, columns=[field, *comparison_statistics(trades)])


def write_comparison_report(output, summary, status):
    """同期間収益差と計算条件を日本語で記録する。"""
    names = {
        'tv_ema_stoch_v1': 'TradingView添付版',
        'lbr_anti_310_adaptation_v1': 'Anti 3/10適応版',
        'lbr_anti_stochastic_v1': 'Anti 7/4/10版',
        'legacy_ema_sma16_v1': '初期試作版',
        'minervini_screen_v1': 'ミネルヴィニ派生版',
        'rsi2_screen_v1': 'RSI2', 'rsi14_screen_v1': 'RSI14',
    }
    lines = [
        '# オルカンとの同額・同期間比較', '',
        f"データ区分は `{status['kind']}`。",
        f"{status['settings'].get('start', '')}〜{status['settings'].get('end', '')}の独立仮想取引について、",
        '各取引の購入日と売却日のオルカン分配金再投資基準価額を使って比較しました。',
        '平均損益率は決済済み取引を等加重した値です。差はパーセントポイントで表示します。', '',
        '| 設定 | 出口 | 決済件数 | 戦略平均 | 同期間オルカン平均 | 平均差 | 差の中央値 | オルカンを上回った比率 |',
        '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |',
    ]
    def percent(value):
        return f'{value:+.2%}' if pd.notna(value) else '対象なし'

    def points(value):
        return f'{value * 100:+.2f}pt' if pd.notna(value) else '対象なし'

    for row in summary.itertuples():
        lines.append(
            f'| {names.get(row.strategy_id, row.strategy_id)} | {row.exit_policy} | {row.trade_count} | '
            f'{percent(row.mean_net_return)} | {percent(row.mean_all_country_return)} | '
            f'{points(row.mean_excess_return)} | {points(row.median_excess_return)} | '
            f'{percent(row.outperform_rate)} |')
    positive = int(summary.mean_excess_return.gt(0).sum())
    lines.extend([
        '', f'平均差がプラスの条件は{positive}/{len(summary)}条件でした。', '',
        '## 計算と費用', '',
        'オルカン損益率＝売却日の分配金再投資基準価額÷購入日の分配金再投資基準価額−1。',
        '差＝既存の費用控除後戦略損益率−オルカン損益率。元本1に対する期末額も取引一覧へ保存しました。',
        f"戦略は既存出口A/B、初期損切り{status['settings'].get('initial_stop', .08):.1%}、"
        f"片道手数料{status['settings'].get('commission', .001):.2%}、"
        f"片道スリッページ{status['settings'].get('slippage', .0005):.2%}を適用済みです。",
        'オルカンはファンド内費用が反映された公式系列を使い、購入・換金の追加費用0、投資家段階の税引前としました。',
        '同日決済はオルカン損益率0として含め、その件数を集計しました。期末未決済は別の一覧へ保存しています。', '',
        '## 比較の範囲', '',
        '株式の寄付・日中決済とオルカンの日次基準価額には時刻差があります。',
        '実ファンドの申込日・翌営業日の適用価額・受渡日を再現するには、別の注文条件が必要になります。',
        '今回の数値は同じ日付で基準価額を参照した取引単位の収益差です。',
        '資金の再利用を伴う年次運用利回りを計算するには、資金配分規則を追加する必要があります。',
        '銘柄・保有期間が重なる取引間の相関、現在の銘柄一覧による生存者・選択バイアス、',
        '元結果の63行の価格範囲補正と仮想約定の条件を含む探索結果として扱います。',
        '年別集計は購入年ごとの取引平均です。', '',
        '## 保存物と出典', '',
        'summary.csvと各条件のtrades.csv、by_ticker.csv、by_entry_year.csv、by_asset_class.csvへ比較結果を保存しています。',
        'positions.csvとrejections.csvは元結果から保存しました。run.jsonに取得元・入力・コードのハッシュを記録しています。',
        f"公式CSVの取得日時は `{status['benchmark_source']['retrieved_at']}`。",
        f'原本は [運用会社の公式CSV]({FUND_URL})。',
        '購入・換金条件は [三菱UFJ銀行の商品情報](https://fs.bk.mufg.jp/webasp/mufg/fund/detail/m00355920.html)を参照しました。',
    ])
    (output / 'REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def run_comparison(source_run, fund_csv, fund_metadata, output):
    """元結果を保持して14条件の比較を別ディレクトリへ保存する。"""
    source_run, output = Path(source_run), Path(output)
    fund_csv, fund_metadata = Path(fund_csv), Path(fund_metadata)
    if output.exists():
        raise ValueError('Use a new output directory')
    parent = json.loads((source_run / 'run.json').read_text(encoding='utf-8-sig'))
    if (parent.get('status') != 'completed' or parent.get('completed_conditions') != 14
            or parent.get('evaluation_method') != 'independent'):
        raise ValueError('Completed 14-condition independent results required')
    provenance = json.loads(fund_metadata.read_text(encoding='utf-8-sig'))
    if (provenance.get('fund_code') != '253425' or provenance.get('source_url') != FUND_URL
            or provenance.get('sha256') != digest(fund_csv) or not provenance.get('retrieved_at')):
        raise ValueError('Fund provenance/hash mismatch')
    nav = load_all_country_nav(fund_csv)
    original = pd.read_csv(source_run / 'summary.csv', float_precision='round_trip')
    pairs = set(zip(original.strategy_id, original.exit_policy))
    if len(original) != 14 or pairs != {(strategy, policy) for strategy in STRATEGIES for policy in ('A', 'B')}:
        raise ValueError('Exactly the seven strategies and exits A/B required')
    output.mkdir(parents=True)
    status = dict(status='running', kind=parent['kind'], evaluation_method='independent',
                  execution_mode=parent['execution_mode'], comparison_method=COMPARISON_METHOD,
                  settings=parent['settings'], benchmark_source=provenance,
                  benchmark_costs=dict(purchase=0, redemption=0, investor_tax=0,
                                       fund_internal_costs='embedded_in_nav'),
                  annualization=False, date_alignment='exact_entry_exit_date_nav',
                  parent_code_commit=parent['code_commit'], parent_code_dirty=parent['code_dirty'],
                  parent_input_hashes=parent['input_hashes'],
                  input_hashes={'parent_run.json': digest(source_run / 'run.json'),
                                'parent_summary.csv': digest(source_run / 'summary.csv'),
                                'fund.csv': digest(fund_csv), 'source.json': digest(fund_metadata)},
                  code_hash=digest(__file__), completed_conditions=0,
                  started_at=pd.Timestamp.now(tz='UTC').isoformat(),
                  warnings=['各取引の購入日・売却日の日次基準価額による参考比較です。',
                            '現在の銘柄一覧、重複期間、価格補正、仮想約定の条件を含みます。'])
    status_path = output / 'run.json'
    summary = []
    try:
        (output / 'benchmark_source.csv').write_bytes(fund_csv.read_bytes())
        (output / 'benchmark_source.json').write_bytes(fund_metadata.read_bytes())
        (output / 'comparison_code.py').write_bytes(Path(__file__).read_bytes())
        nav.to_csv(output / 'benchmark_nav.csv', index_label='Date')
        for original_row in original.itertuples():
            name = f'{original_row.strategy_id}__{original_row.exit_policy}'
            source = source_run / name
            target = output / name
            target.mkdir()
            trades = pd.read_csv(source / 'trades.csv', float_precision='round_trip')
            compared = compare_trades(trades, nav)
            if len(compared) != original_row.trade_count:
                raise ValueError(f'Trade count mismatch: {name}')
            if len(compared) and not np.isclose(compared.net_return.mean(), original_row.mean_net_return,
                                              rtol=0, atol=1e-12):
                raise ValueError(f'Original return mismatch: {name}')
            compared.to_csv(target / 'trades.csv', index=False)
            for field in ('ticker', 'entry_year', 'asset_class'):
                grouped_comparison(compared, field).to_csv(target / f'by_{field}.csv', index=False)
            for filename in ('positions.csv', 'rejections.csv', 'config.json'):
                (target / filename).write_bytes((source / filename).read_bytes())
            for filename in ('trades.csv', 'positions.csv', 'rejections.csv', 'config.json'):
                status['input_hashes'][f'{name}/{filename}'] = digest(source / filename)
            metrics = dict(**comparison_statistics(compared), open_positions=int(original_row.open_positions))
            (target / 'metrics.json').write_text(json.dumps(metrics, indent=2, allow_nan=False), encoding='utf-8')
            summary.append(dict(strategy_id=original_row.strategy_id, exit_policy=original_row.exit_policy, **metrics))
            status['completed_conditions'] = len(summary)
            status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'Compared {len(summary)}/14: {name}', flush=True)
        summary = pd.DataFrame(summary)
        summary.to_csv(output / 'summary.csv', index=False)
        write_comparison_report(output, summary, status)
        status.update(status='completed', completed_at=pd.Timestamp.now(tz='UTC').isoformat())
    except Exception as exc:
        status.update(status='failed', failure=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary
