"""公式一覧を固定し、既存検索関数で全市場を分割評価する。"""
import json
from pathlib import Path
import re
import shutil
import time
import zipfile

import numpy as np
import pandas as pd
import yfinance as yf
from curl_cffi import requests

from .backtest import Config,_validate_execution
from .backtest_signals import STRATEGIES,screening_signals
from .entry_exit_diagnosis import HORIZONS,STOPS,fixed_horizon,entry_statistics
from .exploratory_data import PRICE_COLUMNS,digest
from .independent_backtest import run_independent_backtest

PERIODS={'2016_2020':('2014-01-01','2016-01-04','2020-12-30'),
         '2021_2025':('2019-01-01','2021-01-04','2025-12-30')}


def write_json(path,data):
    Path(path).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')


def read_table(path):
    path=Path(path)
    if path.suffix.lower()=='.xls':
        return pd.read_excel(path,engine='xlrd',dtype=object)
    if path.suffix.lower()=='.csv':
        return pd.read_csv(path,encoding='utf-8-sig',dtype=object)
    raise ValueError('公式一覧は.xlsまたはUTF-8 CSVを指定してください')


def code(value):
    text=str(value).strip().upper().removesuffix('.T')
    if text.endswith('.0') and text[:-2].isdigit():text=text[:-2]
    if not re.fullmatch(r'[0-9][0-9A-Z]{3}',text):
        raise ValueError(f'不明な証券コード: {value}')
    return text+'.T'


def code_column(frame):
    for name in ('コード','銘柄コード','ticker'):
        if name in frame:return name
    raise ValueError('コード列がありません')


def select_assets(listing,etfs):
    if not {'銘柄名','市場・商品区分'}.issubset(listing):
        raise ValueError('JPX一覧の銘柄名・市場・商品区分が必要です')
    etf_codes=[code(v) for v in etfs[code_column(etfs)]]
    if len(etf_codes)!=len(set(etf_codes)) or not etf_codes:
        raise ValueError('ETF一覧が空、またはコードが重複しています')
    confirmed=set(etf_codes); assets=[]; excluded=[]; seen=set(); all_etf=set()
    for row in listing.to_dict('records'):
        ticker=code(row[code_column(listing)])
        if ticker in seen:raise ValueError('JPXコード重複')
        seen.add(ticker)
        market=str(row['市場・商品区分']).strip()
        group=None
        if '内国株式' in market and any(market.startswith(x) for x in ('プライム','スタンダード','グロース')):
            group='stock'
        elif 'ETF' in market:
            all_etf.add(ticker)
            if ticker in confirmed:group='etf'
        if group:
            assets.append(dict(ticker=ticker,name=str(row['銘柄名']),asset_class=group,market=market))
        else:
            excluded.append(dict(ticker=ticker,name=str(row['銘柄名']),market=market,
                                 reason='対象市場外、またはETF公式一覧で未確認'))
    if confirmed-all_etf:
        raise ValueError('ETF一覧にJPX一覧と対応しないコードがあります。原本の日付・対象を確認してください')
    return pd.DataFrame(assets,columns=['ticker','name','asset_class','market']).sort_values('ticker').reset_index(drop=True),pd.DataFrame(excluded)


def freeze(jpx_file,etf_file,output,source_date=None):
    root=Path(output)
    if root.exists():raise ValueError('新規の出力先を指定してください')
    listing=read_table(jpx_file);etfs=read_table(etf_file)
    assets,excluded=select_assets(listing,etfs)
    if assets.empty:raise ValueError('対象銘柄がありません')
    if '日付' in listing:
        dates=listing['日付'].dropna().map(lambda x:str(x).removesuffix('.0')).unique()
        if len(dates)!=1:raise ValueError('JPX一覧の日付が一意ではありません')
        observed=str(pd.to_datetime(dates[0],format='%Y%m%d').date())
        if source_date and source_date!=observed:raise ValueError('指定日付とJPX一覧の日付が不一致')
        source_date=observed
    if not source_date:raise ValueError('一覧の日付がないため--source-dateが必要です')
    source_date=str(pd.Timestamp(source_date).date())
    root.mkdir(parents=True)
    assets.to_csv(root/'universe.csv',index=False,encoding='utf-8-sig')
    excluded.to_csv(root/'excluded_master.csv',index=False,encoding='utf-8-sig')
    master=root/'master';master.mkdir()
    for prefix,p in [('jpx',jpx_file),('etf',etf_file)]:
        shutil.copy2(p,master/f'{prefix}{Path(p).suffix.lower()}')
    manifest=dict(status='frozen',source_date=source_date,frozen_at=pd.Timestamp.now(tz='UTC').isoformat(),
                  universe_hash=digest(root/'universe.csv'),source_hashes={p.name:digest(p) for p in master.iterdir()},
                  asset_count=len(assets),stock_count=int(assets.asset_class.eq('stock').sum()),
                  etf_count=int(assets.asset_class.eq('etf').sum()),periods=PERIODS,
                  caveat='現在の一覧による探索。過去の全上場銘柄を再現していない。')
    write_json(root/'universe.json',manifest)
    return manifest


def frozen_assets(root):
    root=Path(root);meta=json.loads((root/'universe.json').read_text(encoding='utf-8'))
    if digest(root/'universe.csv')!=meta['universe_hash']:raise ValueError('固定一覧が変更されています')
    for name,expected in meta['source_hashes'].items():
        if digest(root/'master'/name)!=expected:raise ValueError('公式原本の保存ファイルが変更されています')
    assets=pd.read_csv(root/'universe.csv',encoding='utf-8-sig')
    if len(assets)!=meta['asset_count'] or assets.ticker.duplicated().any():raise ValueError('一覧件数/重複不整合')
    for row in assets.itertuples():
        if code(row.ticker)!=row.ticker or row.asset_class not in ('stock','etf'):raise ValueError('不明な銘柄区分')
    return meta,assets


def collect(root,limit=100,pause=1.):
    root=Path(root);meta,assets=frozen_assets(root)
    if limit<1 or pause<0:raise ValueError('limit/pauseが不正です')
    cache=root/'cache';cache.mkdir(exist_ok=True)
    state_path=root/'collection.json'
    state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else dict(kind='market',universe_hash=meta['universe_hash'],assets={})
    if state['universe_hash']!=meta['universe_hash']:raise ValueError('取得対象一覧が変わっています')
    tickers=['1306.T']+sorted(set(assets.ticker)-{'1306.T'})
    attempted=0
    with requests.Session(impersonate='chrome') as session:
        for ticker in tickers:
            saved=state['assets'].get(ticker,{})
            if saved.get('status')=='ready':
                for name,hash_value in saved['hashes'].items():
                    if digest(cache/name)!=hash_value:raise ValueError('取得済みファイルが変更されています')
                continue
            if saved.get('status')=='unavailable':continue
            if attempted>=limit:break
            attempted+=1
            try:
                response=session.get(f'https://query2.finance.yahoo.com/v8/finance/chart/{ticker}',
                    params=dict(period1=1388534400,period2=1767225600,interval='1d',events='div,splits'),timeout=30)
                if response.status_code==404:
                    state['assets'][ticker]=dict(status='unavailable',reason='Yahooに履歴なし',checked_at=pd.Timestamp.now(tz='UTC').isoformat())
                    write_json(state_path,state);time.sleep(pause);continue
                response.raise_for_status()
                payload=response.json()
                if payload['chart'].get('error') or not payload['chart'].get('result'):
                    raise ValueError('Yahoo結果が不正です')
                data=payload['chart']['result'][0]
                source=yf.utils.parse_quotes(data)
                if source.empty:raise ValueError('日足が空です')
                source.index=source.index.tz_localize('UTC').tz_convert('Asia/Tokyo').tz_localize(None).normalize()
                source=source.loc['2014-01-01':'2025-12-31']
                source=source.dropna(subset=['Open','High','Low','Close','Volume'],how='all')
                if source.empty:raise ValueError('利用可能な日足がありません')
                if not source.index.is_unique:raise ValueError('日付重複')
                raw=cache/f'{ticker}.json';csv=cache/f'{ticker}.csv'
                raw.write_text(response.text,encoding='utf-8')
                yf.utils.auto_adjust(source).to_csv(csv,index_label='Date',float_format='%.17g')
                state['assets'][ticker]=dict(status='ready',rows=len(source),hashes={p.name:digest(p) for p in (raw,csv)},
                                             retrieved_at=pd.Timestamp.now(tz='UTC').isoformat())
                print(f'保存: {ticker}',flush=True)
            except Exception as exc:
                state['assets'][ticker]=dict(status='error',reason=f'{type(exc).__name__}: {exc}')
                write_json(state_path,state)
                raise
            write_json(state_path,state);time.sleep(pause)
    return state


def quality(frame):
    values=frame[PRICE_COLUMNS]
    bad=(~np.isfinite(values).all(axis=1)) | (values[['Open','High','Low','Close']]<=0).any(axis=1)
    bad |= (frame.High<frame.Low)|(frame.High<frame[['Open','Close']].max(axis=1))|(frame.Low>frame[['Open','Close']].min(axis=1))
    return frame.loc[bad].copy()


def run_batch(root,limit=25,phase="entry"):
    """銘柄単位で保存し、既存の入口/約定/出口関数を変更せず再利用する。"""
    if limit<1:raise ValueError('limitは正整数です')
    if phase not in ('entry','exits'):raise ValueError('phaseはentry/exitsです')
    root=Path(root);meta,assets=frozen_assets(root)
    collection=json.loads((root/'collection.json').read_text(encoding='utf-8'))
    if collection['universe_hash']!=meta['universe_hash']:raise ValueError('取得対象一覧が不一致')
    if collection['assets'].get('1306.T',{}).get('status')!='ready':raise ValueError('RS参照1306.Tの取得が必要です')
    def cached(ticker):
        item=collection['assets'].get(ticker,{})
        if item.get('status')!='ready':raise ValueError('価格が未取得です')
        path=root/'cache'/f'{ticker}.csv'
        if digest(path)!=item['hashes'][path.name]:raise ValueError('価格ハッシュ不一致')
        return pd.read_csv(path,index_col='Date',parse_dates=['Date'],float_precision='round_trip')
    reference=cached('1306.T')
    if not np.isfinite(reference.Close).all() or reference.Close.le(0).any():raise ValueError('RS参照価格が不正です')
    if collection.get('kind') not in ('market','synthetic'):raise ValueError('価格データの区分が不明です')
    current_code={p.name:digest(p) for p in Path(__file__).parent.glob('*.py')}
    results=root/'results';results.mkdir(exist_ok=True)
    performed=0
    for asset in assets.itertuples():
        folder=results/asset.ticker
        old=json.loads((folder/'status.json').read_text(encoding='utf-8')) if (folder/'status.json').exists() else {}
        if old.get('phases') and old.get('code_hashes')!=current_code:
            raise ValueError('前回とコードが異なります。条件を混在させず、新規作業先を使ってください')
        if phase in old.get('phases',[]) or old.get('status','').startswith('blocked'):
            continue
        if performed>=limit:break
        if collection['assets'].get(asset.ticker,{}).get('status')!='ready':continue
        performed+=1;folder.mkdir(exist_ok=True)
        frame=cached(asset.ticker)
        issues=quality(frame)
        if len(issues):
            issues.to_csv(folder/'data_issues.csv',index_label='Date',float_format='%.17g')
            write_json(folder/'status.json',dict(status='blocked_data_quality',issue_rows=len(issues),universe_hash=meta['universe_hash']))
            continue
        entries=[];exits=[]
        archive_path=folder/f'{phase}.zip'
        if archive_path.exists():raise ValueError('未記録の原票ZIPがあります。内容を確認し、別作業先から再開してください')
        with zipfile.ZipFile(archive_path,'x',compression=zipfile.ZIP_DEFLATED) as archive:
            def saved(table,name):
                archive.writestr(name,table.to_csv(index=False,float_format='%.17g'))
            for period,(warmup,start,end) in PERIODS.items():
                prices=frame.loc[warmup:end,PRICE_COLUMNS]
                if prices.loc[start:end].empty:
                    if phase=='entry':
                        entries.extend(dict(period=period,strategy=s,horizon=h,scope=asset.asset_class,count=0,
                                            reason='no_history_in_period',ticker=asset.ticker) for s in STRATEGIES for h in HORIZONS)
                    else:
                        exits.extend(dict(period=period,strategy=s,policy=p,stop=stop,scope=asset.asset_class,count=0,
                                          reason='no_history_in_period',ticker=asset.ticker)
                                     for s in STRATEGIES for p in 'ABCD' for stop in STOPS)
                    continue
                execution=prices.assign(LotSize=1,Split=1.,Dividend=0.,DividendPayDate=None)
                _validate_execution(execution)
                calendar=reference.index.union(prices.index).sort_values()
                cfg=Config(start=start,end=end,time_exit_enabled=False)
                for strategy in STRATEGIES:
                    flags=screening_signals(prices,strategy,reference.loc[warmup:end].Close)
                    period_folder=f'{period}/{strategy}'
                    for horizon in HORIZONS if phase=='entry' else ():
                        outcomes,baseline,excluded=fixed_horizon({asset.ticker:execution},{asset.ticker:flags},calendar,None,horizon,cfg)
                        selected=outcomes[outcomes.is_signal] if len(outcomes) else outcomes
                        row=dict(period=period,strategy=strategy,horizon=horizon,scope=asset.asset_class,ticker=asset.ticker,count=0)
                        if len(selected):row.update(entry_statistics(selected))
                        entries.append(row)
                        saved(selected,f'{period_folder}/fixed_{horizon}.csv')
                        saved(baseline,f'{period_folder}/baseline_{horizon}.csv')
                        saved(excluded,f'{period_folder}/excluded_{horizon}.csv')
                    for stop in STOPS if phase=='exits' else ():
                        for policy in 'ABCD':
                            result=run_independent_backtest({asset.ticker:execution},{asset.ticker:flags},strategy,policy,calendar,
                                {asset.ticker:asset.asset_class},Config(start=start,end=end,time_exit_enabled=False,initial_stop=stop),
                                ratchet_reference_stop=.08)
                            closed=result.trades.net_return;opened=result.positions.unrealized_return
                            combined=pd.concat([closed,opened],ignore_index=True)
                            exits.append(dict(period=period,strategy=strategy,policy=policy,stop=stop,scope=asset.asset_class,
                                ticker=asset.ticker,count=len(combined),mean_return=combined.mean(),median_return=combined.median(),
                                positive_count=int(combined.gt(0).sum()),open_count=len(opened)))
                            target=f'{period_folder}/{policy}_{round(stop*100):02d}'
                            for name,table in [('trades',result.trades),('positions',result.positions),('rejections',result.rejections)]:
                                saved(table,f'{target}/{name}.csv')
        pd.DataFrame(entries if phase=='entry' else exits).to_csv(folder/f'{phase}_summary.csv',index=False)
        phases=sorted(set(old.get('phases',[]))|{phase})
        write_json(folder/'status.json',dict(status='completed' if len(phases)==2 else 'partial',kind=collection['kind'],phases=phases,universe_hash=meta['universe_hash'],
                   artifact_hashes={p.name:digest(p) for p in folder.iterdir() if p.suffix in ('.zip','.csv')},
                   input_hash=collection['assets'][asset.ticker]['hashes'][f'{asset.ticker}.csv'],
                   code_hashes=current_code))
        print(f'検証保存: {asset.ticker}',flush=True)
    return performed


def aggregate(root,phase="entry"):
    if phase not in ("entry","exits"):raise ValueError("phaseはentry/exitsです")
    root=Path(root);meta,assets=frozen_assets(root)
    entries=[];exits=[];coverage=[];versions=set();kinds=set()
    for asset in assets.itertuples():
        folder=root/'results'/asset.ticker
        status=json.loads((folder/'status.json').read_text(encoding='utf-8')) if (folder/'status.json').exists() else dict(status='pending')
        done=phase in status.get('phases',[])
        coverage.append(dict(ticker=asset.ticker,asset_class=asset.asset_class,status='completed' if done else status['status']))
        if done:
            if status['universe_hash']!=meta['universe_hash']:raise ValueError('結果の母集団不一致')
            versions.add(json.dumps(status['code_hashes'],sort_keys=True));kinds.add(status['kind'])
            if digest(root/'cache'/f'{asset.ticker}.csv')!=status['input_hash']:raise ValueError('計算後に価格キャッシュが変更されています')
            for name,expected_hash in status['artifact_hashes'].items():
                if digest(folder/name)!=expected_hash:raise ValueError('保存結果のハッシュが不一致')
            (entries if phase=='entry' else exits).append(pd.read_csv(folder/f'{phase}_summary.csv'))
    if len(versions)>1 or len(kinds)>1:raise ValueError('異なるコード/データ区分の結果を混在させられません')
    out=root/f'summary_{phase}';out.mkdir(exist_ok=True)
    pd.DataFrame(coverage).to_csv(out/'coverage.csv',index=False)
    for filename,frames,keys,metrics in [
        ('entry_summary.csv',entries,['period','strategy','horizon','scope'],['mean_return','timing_advantage','mean_mae','mean_mfe']),
        ('exit_summary.csv',exits,['period','strategy','policy','stop','scope'],['mean_return'])]:
        rows=[]
        if frames:
            all_rows=pd.concat(frames,ignore_index=True)
            for values,group in all_rows.groupby(keys):
                eligible=group[group['count']>0]
                row=dict(zip(keys,values));row['count']=int(eligible['count'].sum());row['ticker_count']=len(eligible)
                for metric in metrics:
                    good=eligible[eligible.get(metric,pd.Series(index=eligible.index,dtype=float)).notna()]
                    row[metric]=float(np.average(good[metric],weights=good['count'])) if len(good) else None
                rows.append(row)
        pd.DataFrame(rows).to_csv(out/filename,index=False)
    complete=all(row['status']=='completed' for row in coverage)
    report=dict(status='completed' if complete else 'partial',phase=phase,kind=next(iter(kinds)) if kinds else 'unrun',universe_hash=meta['universe_hash'],
                total=len(coverage),completed=sum(r['status']=='completed' for r in coverage),
                blocked=sum(r['status'].startswith('blocked') for r in coverage),
                warning='一部結果を全市場の完了結果と解釈しない。独立取引平均はCAGRではない。')
    write_json(out/'status.json',report)
    return report
