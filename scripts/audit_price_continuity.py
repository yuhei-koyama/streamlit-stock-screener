#!/usr/bin/env python3
"""保存済み全市場価格の連続性を監査し、既存取引との重なりを保存する。"""
import argparse
from hashlib import sha256
import io
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock_screener.market_research import PERIODS, write_json

THRESHOLDS = dict(review_price_factor=1.5, extreme_price_factor=10., long_missing_sessions=20,
                  zero_volume_absolute_price=100_000_000., nav_absolute_daily_return=.5)
PRICE_COLUMNS = ['Open', 'High', 'Low', 'Close', 'Volume']
EVENT_COLUMNS = ['event_type', 'start_date', 'end_date', 'previous_date', 'previous_close',
                 'current_close', 'price_factor', 'missing_sessions', 'priority']


def symmetric_factor(current, previous):
    ratio = current / previous
    return pd.concat([ratio, 1 / ratio], axis=1).max(axis=1)


def scan_prices(frame, calendar):
    """未来側の価格は監査の比較にだけ使用し、価格や売買判定を変更しない。"""
    if frame.empty or not frame.index.is_unique or not frame.index.is_monotonic_increasing:
        raise ValueError('価格の日付または件数が不正です')
    if not frame.index.isin(calendar).all():
        raise ValueError('承認済みカレンダー外の日付があります')
    prices = frame[PRICE_COLUMNS]
    finite = np.isfinite(prices).all(axis=1)
    valid = finite & frame[['Open', 'High', 'Low', 'Close']].gt(0).all(axis=1) & frame.Volume.ge(0)
    ohlc_bad = frame.High.lt(frame[['Open', 'Close', 'Low']].max(axis=1))
    ohlc_bad |= frame.Low.gt(frame[['Open', 'Close', 'High']].min(axis=1))
    invalid = frame[~valid | ohlc_bad].copy()
    close = frame.Close.where(valid)
    dates = pd.Series(frame.index, index=frame.index)
    previous_close = close.shift(1)
    previous_date = dates.shift(1)
    factor = symmetric_factor(close, previous_close)
    observed_session = pd.Series(calendar.get_indexer(frame.index), index=frame.index)
    trade = valid & frame.Volume.gt(0)
    previous_trade_close = close.where(trade).ffill().shift(1)
    next_trade_close = close.where(trade).bfill().shift(-1)
    zero = frame[frame.Volume.eq(0)].copy()
    zero['previous_tradable_date'] = dates.where(trade).ffill().shift(1).reindex(zero.index)
    zero['previous_tradable_close'] = previous_trade_close.reindex(zero.index)
    zero['next_tradable_date'] = dates.where(trade).bfill().shift(-1).reindex(zero.index)
    zero['next_tradable_close'] = next_trade_close.reindex(zero.index)
    zero['factor_to_previous_tradable'] = symmetric_factor(close, previous_trade_close).reindex(zero.index)
    zero['factor_to_next_tradable'] = symmetric_factor(close, next_trade_close).reindex(zero.index)
    zero['anchor_factor'] = zero[['factor_to_previous_tradable','factor_to_next_tradable']].max(axis=1)
    zero['review_candidate'] = zero.anchor_factor.ge(THRESHOLDS['review_price_factor']) | zero.Close.ge(THRESHOLDS['zero_volume_absolute_price'])
    events = []
    def add_events(mask, kind, start, prev_dates, prev_closes, factors, missing=None):
        selected = pd.DataFrame({'event_type':kind, 'start_date':start, 'end_date':dates,
            'previous_date':prev_dates,'previous_close':prev_closes,'current_close':close,
            'price_factor':factors,'missing_sessions':missing if missing is not None else 0},index=frame.index).loc[mask].copy()
        selected['priority'] = np.where(selected.price_factor.ge(THRESHOLDS['extreme_price_factor']), 'extreme_scale', 'review')
        events.append(selected.reset_index(drop=True))
    add_events(factor.ge(THRESHOLDS['review_price_factor']), 'close_discontinuity', dates, previous_date, previous_close, factor)
    within_factor = frame[['Open','High','Low','Close']].max(axis=1)/frame[['Open','High','Low','Close']].min(axis=1)
    add_events(within_factor.ge(THRESHOLDS['review_price_factor']), 'wide_intraday_range', dates, previous_date, previous_close, within_factor)
    zero_mask = zero.review_candidate.reindex(frame.index,fill_value=False)
    zero_factor = zero.anchor_factor.reindex(frame.index)
    add_events(zero_mask, 'zero_volume_price_dislocation', dates,
               zero.previous_tradable_date.reindex(frame.index), previous_trade_close, zero_factor)
    if len(events[-1]):
        absolute_extreme = events[-1].current_close.ge(THRESHOLDS['zero_volume_absolute_price'])
        events[-1].loc[absolute_extreme,'priority'] = 'extreme_scale'
    missing = observed_session.diff()-1
    gap_start = previous_date.map(lambda value: calendar[calendar.get_loc(value)+1] if pd.notna(value) else pd.NaT)
    add_events(missing.ge(THRESHOLDS['long_missing_sessions']), 'missing_price_rows', gap_start,
               previous_date, previous_close, factor, missing)
    trade_positions = observed_session.where(trade).ffill().shift(1)
    missing_trade = observed_session-trade_positions-1
    prev_trade_dates = dates.where(trade).ffill().shift(1)
    trade_gap_start = prev_trade_dates.map(lambda value: calendar[calendar.get_loc(value)+1] if pd.notna(value) else pd.NaT)
    trade_factor = symmetric_factor(close,previous_trade_close)
    add_events(trade & missing_trade.ge(THRESHOLDS['long_missing_sessions']), 'no_positive_volume_rows',
               trade_gap_start,prev_trade_dates,previous_trade_close,trade_factor,missing_trade)
    # 初回の正出来高まで続く区間も、観測開始からの日数として記録する。
    if trade.any() and not bool(trade.iloc[0]):
        first_trade = frame.index[trade][0]
        leading = int(calendar.get_loc(first_trade)-calendar.get_loc(frame.index[0]))
        if leading >= THRESHOLDS['long_missing_sessions']:
            first_ratio = float(close.loc[first_trade]/close.iloc[0])
            events.append(pd.DataFrame([dict(event_type='leading_no_positive_volume',
                start_date=frame.index[0],end_date=first_trade,
                previous_date=frame.index[0],previous_close=close.iloc[0],current_close=close.loc[first_trade],
                price_factor=max(first_ratio,1/first_ratio),missing_sessions=leading,priority='review')]))
    # 期末まで再出現しない出来高0の連続区間も記録する。
    if trade.any() and not bool(trade.iloc[-1]):
        last_trade = frame.index[trade][-1]
        trailing = int(calendar.get_loc(frame.index[-1])-calendar.get_loc(last_trade))
        if trailing >= THRESHOLDS['long_missing_sessions']:
            events.append(pd.DataFrame([dict(event_type='trailing_no_positive_volume',
                start_date=calendar[calendar.get_loc(last_trade)+1],end_date=frame.index[-1],
                previous_date=last_trade,previous_close=close.loc[last_trade],current_close=close.iloc[-1],
                price_factor=float(trade_factor.iloc[-1]),missing_sessions=trailing,priority='review')]))
    if not trade.any():
        events.append(pd.DataFrame([dict(event_type='no_positive_volume_history',start_date=frame.index[0],
            end_date=frame.index[-1],previous_date=pd.NaT,previous_close=np.nan,current_close=close.iloc[-1],
            price_factor=np.nan,missing_sessions=len(frame),priority='review')]))
    nonempty_events = [event for event in events if not event.empty]
    all_events = pd.concat(nonempty_events,ignore_index=True)[EVENT_COLUMNS] if nonempty_events else pd.DataFrame(columns=EVENT_COLUMNS)
    return zero, all_events, invalid


def read_verified(path, expected):
    content=path.read_bytes()
    if sha256(content).hexdigest()!=expected:
        raise ValueError('保存済みファイルのハッシュが一致しません。'+str(path))
    return content


def interval_overlaps(intervals, events):
    records=[]
    if intervals.empty or events.empty:
        return records
    grouped=events.groupby('ticker',sort=False)
    for ticker, trades in intervals.groupby('ticker',sort=False):
        if ticker not in grouped.groups:
            continue
        ticker_events=grouped.get_group(ticker)
        for trade in trades.itertuples():
            overlapping=ticker_events[ticker_events.start_date.le(trade.exit_date)&ticker_events.end_date.ge(trade.entry_date)]
            for event in overlapping.itertuples():
                records.append(dict(event_id=event.event_id,ticker=ticker,trade_id=trade.trade_id,
                    entry_date=trade.entry_date,exit_or_valuation_date=trade.exit_date,
                    position_status=trade.position_status,event_type=event.event_type,priority=event.priority,
                    event_start=event.start_date,event_end=event.end_date,
                    overlap_interpretation='holding_interval_overlap_not_proven_causal_pnl'))
    return records


def audit(root,output):
    root,output=Path(root),Path(output)
    if output.exists():
        raise ValueError('新しい監査出力先を指定してください')
    data=root/'prepared-primary-01'
    run=root/'primary-run-20261107-01'
    meta=json.loads((run/'run.json').read_text(encoding='utf-8'))
    result_verification=json.loads((root/'primary-analysis-01/verification.json').read_text(encoding='utf-8'))
    if meta['status']!='completed' or meta['completed_cases']!=560:
        raise ValueError('完了した560条件が必要です')
    if sha256((run/'run.json').read_bytes()).hexdigest()!=result_verification['source_run_sha256']:
        raise ValueError('元の実行メタデータが一致しません')
    manifest_content=read_verified(data/'manifest.json',meta['input_manifest_sha256'])
    manifest=json.loads(manifest_content)
    if manifest['file_hashes']!=meta['input_hashes'] or len(manifest['prepared_assets'])!=4027:
        raise ValueError('本実行と準備済み入力の対応が一致しません')
    output.mkdir(parents=True)
    write_json(output/'status.json',dict(status='running',checked_assets=0,checked_cases=0,thresholds=THRESHOLDS))
    try:
        for name,expected in manifest['file_hashes'].items():
            read_verified(data/name,expected)
        collection_content=(root/'universe/collection.json').read_bytes()
        collection=json.loads(collection_content)
        raw_hashes={}
        calendar=pd.DatetimeIndex(pd.read_csv(data/'calendar.csv',parse_dates=['Date']).Date)
        coverage,zeros,events,invalids=[],[],[],[]
        for count,asset in enumerate(manifest['prepared_assets'],1):
            ticker=asset['ticker']
            saved=collection['assets'][ticker]
            for name,expected in saved['hashes'].items():
                read_verified(root/'universe/cache'/name,expected)
                raw_hashes[name]=expected
            content=read_verified(data/f'signals_{ticker}.csv',manifest['file_hashes'][f'signals_{ticker}.csv'])
            frame=pd.read_csv(io.BytesIO(content),index_col='Date',parse_dates=['Date'],float_precision='round_trip')
            zero,price_events,invalid=scan_prices(frame,calendar)
            identity=dict(ticker=ticker,name=asset['name'],asset_class=asset['asset_class'],
                          source_sha256=sha256(content).hexdigest())
            zeros.append(zero.reset_index().assign(**identity))
            events.append(price_events.assign(**identity))
            invalids.append(invalid.reset_index().assign(**identity))
            coverage.append(dict(identity,rows=len(frame),zero_volume_rows=len(zero),
                zero_volume_review_rows=int(zero.review_candidate.sum()),review_events=len(price_events),
                extreme_events=int(price_events.priority.eq('extreme_scale').sum()),
                invalid_price_rows=len(invalid),first_date=frame.index[0],last_date=frame.index[-1],
                positive_volume_rows=int(frame.Volume.gt(0).sum())))
            if count%250==0:
                write_json(output/'status.json',dict(status='running',checked_assets=count,checked_cases=0,thresholds=THRESHOLDS))
                print('価格監査',count,'/ 4027',flush=True)
        coverage=pd.DataFrame(coverage)
        zeros=pd.concat(zeros,ignore_index=True)
        events=pd.concat([frame for frame in events if not frame.empty],ignore_index=True)
        invalids=pd.concat(invalids,ignore_index=True)
        events.insert(0,'event_id',[f'E{number:06d}' for number in range(1,len(events)+1)])
        for name,frame in [('coverage',coverage),('zero_volume_rows',zeros),('review_events',events),('invalid_price_rows',invalids)]:
            frame.to_csv(output/(name+'.csv'),index=False,float_format='%.17g',encoding='utf-8-sig')
        case_records,overlaps,nav_events=[],[],[]
        case_paths=sorted(run.glob('*/*/*/*/metrics.json'))
        if len(case_paths)!=560:
            raise ValueError('既存560条件の保存先が一致しません')
        result_hashes={}
        for count,path in enumerate(case_paths,1):
            folder=path.parent
            period,group,strategy,policy=folder.relative_to(run).parts
            identity=dict(period=period,asset_class=group,strategy_id=strategy,
                          exit_policy=policy.split('_')[0],initial_stop=float(policy.split('_')[-1]))
            frames={}
            for filename in ['daily.csv','trades.csv','positions.csv']:
                name=(folder/filename).relative_to(run).as_posix()
                expected=result_verification['source_result_hashes'][name]
                content=read_verified(folder/filename,expected)
                result_hashes[name]=expected
                frames[filename]=pd.read_csv(io.BytesIO(content),float_precision='round_trip')
            daily=frames['daily.csv']
            daily.date=pd.to_datetime(daily.date)
            start,end=daily.date.iloc[0],daily.date.iloc[-1]
            warmup=pd.Timestamp(PERIODS[period][0])
            candidate_events=events[(events.asset_class.eq(group)|events.ticker.eq('1306.T'))&events.start_date.le(end)&events.end_date.ge(warmup)]
            evaluation_events=candidate_events[candidate_events.end_date.ge(start)]
            intervals=frames['trades.csv'][['ticker','trade_id','entry_date','exit_date']].copy()
            intervals['position_status']='closed'
            opened=frames['positions.csv'][['ticker','trade_id','entry_date']].copy()
            opened['exit_date']=end
            opened['position_status']='open_at_end'
            intervals=pd.concat([intervals,opened],ignore_index=True)
            intervals['entry_date']=pd.to_datetime(intervals.entry_date)
            intervals['exit_date']=pd.to_datetime(intervals.exit_date)
            found=interval_overlaps(intervals,evaluation_events)
            overlaps.extend(dict(identity,**row) for row in found)
            nav_returns=daily.nav.pct_change(fill_method=None)
            abnormal=daily[nav_returns.abs().gt(THRESHOLDS['nav_absolute_daily_return'])].copy()
            abnormal['daily_return']=nav_returns[nav_returns.abs().gt(THRESHOLDS['nav_absolute_daily_return'])]
            nav_events.extend(dict(identity,**row) for row in abnormal.to_dict('records'))
            case_records.append(dict(identity,input_review_events=len(candidate_events),
                evaluation_review_events=len(evaluation_events),input_review_tickers=candidate_events.ticker.nunique(),
                holding_overlap_events=len(set(row['event_id'] for row in found)),
                holding_overlap_trades=len(set(row['trade_id'] for row in found)),
                extreme_holding_overlap_events=len(set(row['event_id'] for row in found if row['priority']=='extreme_scale')),
                extreme_holding_tickers='|'.join(sorted(set(row['ticker'] for row in found if row['priority']=='extreme_scale'))),
                nav_review_rows=len(abnormal),max_daily_return=float(nav_returns.max()),
                min_daily_return=float(nav_returns.min()),max_nav=float(daily.nav.max()),final_nav=float(daily.nav.iloc[-1])))
            if count%70==0:
                write_json(output/'status.json',dict(status='running',checked_assets=4027,checked_cases=count,thresholds=THRESHOLDS))
                print('影響照合',count,'/ 560',flush=True)
        cases=pd.DataFrame(case_records)
        overlap_columns=[*identity,'event_id','ticker','trade_id','entry_date','exit_or_valuation_date',
                         'position_status','event_type','priority','event_start','event_end','overlap_interpretation']
        overlap_frame=pd.DataFrame(overlaps,columns=overlap_columns)
        nav_frame=pd.DataFrame(nav_events)
        for name,frame in [('case_impact_summary',cases),('holding_interval_overlaps',overlap_frame),('nav_review_events',nav_frame)]:
            frame.to_csv(output/(name+'.csv'),index=False,float_format='%.17g',encoding='utf-8-sig')
        source_counts=events.groupby(['ticker','name','asset_class']).agg(review_events=('event_id','size'),
            extreme_events=('priority',lambda values:int(values.eq('extreme_scale').sum()))).reset_index()
        if not overlap_frame.empty:
            direct=overlap_frame.groupby('ticker').agg(holding_overlaps=('event_id','size'),
                overlapping_conditions=('strategy_id','size')).reset_index()
            # 同じ条件に複数の行・取引が重なっても、条件数は一度だけ数える。
            distinct=overlap_frame.drop_duplicates(['ticker','period','asset_class','strategy_id','exit_policy','initial_stop']).groupby('ticker').size()
            direct['overlapping_conditions']=direct.ticker.map(distinct)
            source_counts=source_counts.merge(direct,on='ticker',how='left')
        source_counts.to_csv(output/'ticker_review_summary.csv',index=False,encoding='utf-8-sig')
        summary=dict(status='completed_review_candidates',checked_assets=4027,checked_cases=560,
            checked_prepared_files=len(manifest['file_hashes']),checked_original_files=len(raw_hashes),
            checked_result_files=len(result_hashes),price_rows=int(coverage.rows.sum()),
            zero_volume_rows=len(zeros),zero_volume_tickers=int(coverage.zero_volume_rows.gt(0).sum()),
            zero_volume_review_rows=int(coverage.zero_volume_review_rows.sum()),
            review_events=len(events),review_tickers=int(events.ticker.nunique()),
            extreme_events=int(events.priority.eq('extreme_scale').sum()),
            extreme_tickers=sorted(events.loc[events.priority.eq('extreme_scale'),'ticker'].unique()),
            invalid_price_rows=len(invalids),holding_overlap_records=len(overlap_frame),
            cases_with_holding_overlap=int(cases.holding_overlap_events.gt(0).sum()),
            cases_with_extreme_holding_overlap=int(cases.extreme_holding_overlap_events.gt(0).sum()),
            nav_review_rows=len(nav_frame),cases_with_nav_review=int(cases.nav_review_rows.gt(0).sum()),
            event_type_counts=events.event_type.value_counts().to_dict(),thresholds=THRESHOLDS,
            input_manifest_sha256=meta['input_manifest_sha256'],source_run_sha256=result_verification['source_run_sha256'],
            collection_sha256=sha256(collection_content).hexdigest(),
            interpretation='review_candidates_require_evidence_not_automatic_price_corrections')
        (output/'source_manifest.json').write_bytes(manifest_content)
        write_json(output/'source_original_hashes.json',raw_hashes)
        write_json(output/'source_result_hashes.json',result_hashes)
        (output/'audit_code.py').write_bytes(Path(__file__).read_bytes())
        lines=['# 保存済み全市場価格の追加品質監査','',
            f"4,027銘柄・{summary['price_rows']:,}行を監査し、全560条件の取引・保有・日次純資産と照合した。準備済み{summary['checked_prepared_files']:,}ファイル、原本{summary['checked_original_files']:,}ファイル、結果{summary['checked_result_files']:,}ファイルのハッシュが保存記録と一致した。",'',
            '## 抽出条件と解釈','',
            '監査の抽出値は、前後終値・日中高安の倍率1.5倍以上、極端な倍率10倍以上、20営業日以上の行欠損・正出来高なし期間、出来高0かつ近隣の正出来高価格から1.5倍以上乖離または価格1億円以上、純資産の絶対日次損益率50%超。倍率は上昇・下落を対称に評価する。', '',
            '候補は正常な急騰落、低流動性、上場休止、企業行動、取得欠損等を含み得る。抽出件数を誤価格の確定件数としては扱わない。正出来高価格の未来側は今回の事後監査の比較にだけ用いる。比較先が数年離れる場合は通常の長期値動きも倍率に含むため、倍率だけで誤価格と判定しない。', '',
            '## 集計結果','',
            f"- 出来高0は{len(zeros):,}行・{summary['zero_volume_tickers']:,}銘柄。そのうち価格乖離の要確認は{summary['zero_volume_review_rows']:,}行。",
            f"- 要確認イベントは{len(events):,}件・{summary['review_tickers']:,}銘柄。複数の抽出理由が同じ日付を指すことがある。",
            f"- 極端な倍率・価格の候補は{summary['extreme_events']:,}件。該当銘柄は{'、'.join(summary['extreme_tickers'])}。",
            f"- 非正価格・非有限値・負出来高・OHLC範囲不整合は{len(invalids):,}行。",
            f"- 保有区間と候補が重なるのは{summary['cases_with_holding_overlap']:,}条件、極端候補との重なりは{summary['cases_with_extreme_holding_overlap']:,}条件。",
            f"- 純資産の絶対日次損益率50%超は{summary['nav_review_rows']:,}行・{summary['cases_with_nav_review']:,}条件。",'',
            '### 抽出理由の内訳','', '| 抽出理由 | イベント件数 |','| --- | ---: |']
        labels={'close_discontinuity':'前後終値の不連続','wide_intraday_range':'日中価格レンジ拡大',
                'zero_volume_price_dislocation':'出来高0の価格乖離','missing_price_rows':'20営業日以上の価格行欠損',
                'no_positive_volume_rows':'20営業日以上の正出来高なし期間後の再出現',
                'trailing_no_positive_volume':'期末まで続く20営業日以上の正出来高なし期間',
                'leading_no_positive_volume':'観測開始から続く20営業日以上の正出来高なし期間',
                'no_positive_volume_history':'保存全期間に正出来高なし'}
        for kind,count in summary['event_type_counts'].items():
            lines.append(f'| {labels[kind]} | {count:,} |')
        lines+=['','## 影響条件の読み方','',
            'case_impact_summary.csvは全560条件。input_review_eventsは準備期間も含む入力候補の数、holding_overlap_eventsは評価期間中の保有区間との重なりを示す。1306.Tの候補はRS参照として両資産区分の入力影響に含める。', '',
            'holding_interval_overlaps.csvは入口日から売却日または期末までと要確認イベント区間が重なる記録。売却日・購入日を含むため、実際にその行で約定・終値評価したかや、損益への因果的影響を確定するものではない。', '',
            '未保有の候補でも指標、買いシグナル、候補順位、資金配分へ影響する可能性がある。保有区間の重なりが0であることだけでは影響なしと判定できない。初回観測前・最終観測後の価格欠損を上場前後と断定する検査は含まない。', '',
            '## 保存物','',
            'coverage.csvは全銘柄、zero_volume_rows.csvは出来高0の全行、review_events.csvは要確認イベント、ticker_review_summary.csvは銘柄別集計。nav_review_events.csvは純資産急変、invalid_price_rows.csvは基本価格検査。元価格・元結果は維持し、ハッシュと監査コードを保存した。', '',
            '本監査は候補の抽出と影響範囲の照合まで。候補ごとの確定判断、上場廃止・再上場時の保有精算、価格補正・銘柄除外・再実行は別の判断として扱う。']
        (output/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
        write_json(output/'summary.json',summary)
        write_json(output/'verification.json',dict(status='completed',
            output_hashes={p.name:sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file() and p.name!='status.json'},
            source_input_hashes=manifest['file_hashes'],source_code_sha256=sha256(Path(__file__).read_bytes()).hexdigest()))
        write_json(output/'status.json',dict(status='completed_review_candidates',checked_assets=4027,checked_cases=560))
        print(json.dumps(summary,ensure_ascii=False),flush=True)
        return summary
    except Exception as exc:
        write_json(output/'status.json',dict(status='failed',error=str(exc)))
        raise


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    audit(args.root,args.output)