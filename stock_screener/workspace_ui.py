"""銘柄検索・詳細・検証結果を提供する日本語ワークスペース。"""
from dataclasses import asdict
import hashlib
import json
import re
import time

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .backtest_signals import MINERVINI, RSI2, RSI14, STRATEGIES
from .browser_profiles import PROFILES
from .linda_versions import TV, LEGACY, ANTI_310, ANTI_STOCH, TV_DEFAULTS
from .screening_workspace import (REFERENCE, NAMES, DailyStore, SearchSettings, decision_date,
    evaluate_entry, exit_projection, indicator_prices, purchase_estimate, scan_catalog, write_json)
from .screening_workspace import refresh_calendar
from .screening_workspace import price_digest
from .screening_workspace import required_history_years
from .screening_workspace import screening_code_hash


DEFAULTS = dict(search_scope='東証普通株', search_codes='7203, 6758, 1306',
    search_strategies=[TV], search_mode='OR', search_turnover=10., search_limit=100,
    search_liquidity_enabled=True,
    search_budget_enabled=False, search_budget=100000., search_exit='A', search_stop=10.,
    search_commission=0., search_slippage=.05)


def catalog():
    path = REFERENCE/'universe.csv'
    metadata = json.loads((REFERENCE/'universe.json').read_text(encoding='utf-8'))
    if hashlib.sha256(path.read_bytes()).hexdigest()!=metadata['csv_sha256']:
        raise ValueError('検索用上場一覧と保存記録のハッシュが一致しません')
    frame = pd.read_csv(path)
    if frame.ticker.duplicated().any():
        raise ValueError('検索用上場一覧にコードの重複があります')
    return frame


def saved_settings():
    values = {key: st.session_state.get(key, value) for key, value in DEFAULTS.items()} | {
        key: value for key, value in st.session_state.items() if key.startswith('param_')}
    if st.session_state.get('requested_date'):
        values['requested_date'] = str(st.session_state['requested_date'])
    return values


def browser_controls():
    value = PROFILES(data={'settings': saved_settings()}, key='browser_profiles',
                     on_restore_change=lambda: None, on_client_change=lambda: None,
                     on_mobile_change=lambda: None)
    if value.get('restore'):
        restored = value['restore'].get('settings', {})
        if isinstance(restored, dict):
            for key, item in restored.items():
                if key in DEFAULTS or key.startswith('param_'):
                    st.session_state[key] = item
                elif key=='requested_date':
                    try:
                        st.session_state[key] = pd.Timestamp(item).date()
                    except (ValueError,TypeError):
                        st.warning('保存した判定基準日を確認できませんでした。')
        st.session_state.pop('use_today', None)
        st.session_state.pop('search_output', None)
    client = value.get('client')
    if isinstance(client, str) and re.fullmatch(r'[a-f0-9-]{36}', client):
        st.session_state['browser_client'] = client
        if not st.session_state.get('loaded_browser_result'):
            path = DailyStore().root/'results'/f'{client}.json'
            if path.exists():
                st.session_state['search_output'] = json.loads(path.read_text(encoding='utf-8'))
            st.session_state['loaded_browser_result'] = True
    st.session_state['mobile_view'] = bool(value.get('mobile', False))


def parameters(strategies):
    settings = {}
    with st.expander('入口の詳細設定'):
        st.caption('変更した設定は、保存済み検証条件との差として記録します。')
        if TV in strategies:
            st.markdown('**TradingView添付版**')
            values = {}
            labels = dict(fast_len='短期EMA', slow_len='長期EMA', signal_len='シグナルEMA',
                          k_len='ストキャス期間', d_len='D平滑化', smooth_k='K平滑化')
            columns = st.columns(3)
            for i, (key, default) in enumerate(TV_DEFAULTS.items()):
                values[key] = columns[i % 3].number_input(labels[key], 1, 500, default, key='param_tv_'+key)
            if values != TV_DEFAULTS:
                settings[TV] = values
        if ANTI_310 in strategies:
            count = st.selectbox('Anti 3/10の押し期間', [3, 2], key='param_anti_pullback')
            cross = st.checkbox('Anti 3/10のFast通過を許可', True, key='param_anti_cross')
            if count != 3 or not cross:
                settings[ANTI_310] = dict(pullback_bars=count, allow_cross=cross)
        for strategy,defaults,labels in [
            (ANTI_STOCH,dict(k_len=7,fast_len=4,slow_len=10),dict(k_len='ストキャス期間',fast_len='Fast平滑化',slow_len='Slow平滑化')),
            (LEGACY,dict(fast_len=3,slow_len=10,signal_sma=16,price_sma=16,min_rows=30),
             dict(fast_len='短期EMA',slow_len='長期EMA',signal_sma='シグナルSMA',price_sma='株価SMA',min_rows='最小準備本数'))]:
            if strategy in strategies:
                st.markdown('**'+NAMES[strategy]+'**')
                columns = st.columns(3)
                values = {key:columns[i%3].number_input(labels[key],1,500,default,key='param_'+strategy+'_'+key)
                          for i,(key,default) in enumerate(defaults.items())}
                if values!=defaults:
                    settings[strategy] = values
        if MINERVINI in strategies:
            st.markdown('**ミネルヴィニ派生版**')
            defaults = dict(volume_multiplier=1.5,low_multiplier=1.3,high_ratio=.75,minimum_rs=1.)
            labels = dict(volume_multiplier='20日平均出来高に対する倍率',low_multiplier='252日安値に対する倍率',
                          high_ratio='252日高値に対する比率',minimum_rs='RS126の下限')
            columns = st.columns(2)
            values = {key:columns[i%2].number_input(labels[key],min_value=.01,max_value=1. if key=='high_ratio' else 100.,
                value=default,step=.05,key='param_minervini_'+key) for i,(key,default) in enumerate(defaults.items())}
            if values!=defaults:
                settings[MINERVINI] = values
        for strategy, default in [(RSI2, 10.), (RSI14, 30.)]:
            if strategy in strategies:
                threshold = st.number_input(NAMES[strategy]+'の閾値未満', 0., 100., default, key='param_'+strategy+'_threshold')
                trend = st.checkbox(NAMES[strategy]+'で終値>SMA200を要求', True, key='param_'+strategy+'_trend')
                if threshold != default or not trend:
                    settings[strategy] = dict(threshold=threshold, trend_filter=trend)
    return settings


def controls():
    left, right = st.columns([2, 1])
    with left:
        scope = st.segmented_control('検索対象', ['東証普通株', '国内上場ETF', 'コード指定'], default='東証普通株', key='search_scope')
        if scope is None:
            raise ValueError('検索対象を選択してください')
        strategies = st.multiselect('入口ロジック', STRATEGIES, default=[TV], format_func=NAMES.get, key='search_strategies')
    with right:
        st.radio('複数入口の組合せ', ['OR', 'AND'], horizontal=True, key='search_mode',
                 format_func=lambda x: 'いずれか成立' if x == 'OR' else 'すべて成立')
        today = pd.Timestamp.now(tz='Asia/Tokyo').date()
        first_date = pd.Timestamp('2014-01-01').date()
        previous_date = st.session_state.get('requested_date')
        if previous_date is not None and not first_date<=previous_date<=today:
            st.session_state['requested_date'] = min(today,max(first_date,previous_date))
        requested = st.date_input('判定基準日', today, min_value=first_date,max_value=today,key='requested_date')
        use_today = st.checkbox('今回の検索で当日値を利用する', key='use_today', help='当日値は配信時点の暫定値です。')
    selected_catalog = catalog()
    if scope == 'コード指定':
        from app import normalize_tickers
        codes = st.text_area('東証コード', key='search_codes')
        tickers, invalid = normalize_tickers(codes)
        if invalid:
            raise ValueError('無効なコード　'+'、'.join(invalid))
        unknown = set(tickers)-set(selected_catalog.ticker)
        if unknown:
            st.warning('同梱の上場一覧で確認待ち　'+'、'.join(sorted(unknown)))
            extra = pd.DataFrame([dict(ticker=t,name=t,asset_class='unknown',lot_size=np.nan) for t in sorted(unknown)])
            selected_catalog = pd.concat([selected_catalog,extra],ignore_index=True)
        selected_catalog = selected_catalog[selected_catalog.ticker.isin(tickers)]
    else:
        selected_catalog = selected_catalog[selected_catalog.asset_class.eq('stock' if scope == '東証普通株' else 'etf')]
    with st.expander('流動性・件数・購入予算', expanded=True):
        liquidity_enabled = st.checkbox('流動性条件で絞り込む',value=True,key='search_liquidity_enabled',help='OFFでは売買代金の下限と上位件数の制限を外し、選択した対象全体で入口条件を判定します。')
        c1, c2, c3 = st.columns(3)
        turnover = c1.number_input('20営業日平均売買代金の下限（億円）', 0., 10000., value=10., step=1., key='search_turnover',disabled=not liquidity_enabled)
        limit = c2.number_input('売買代金順の最大銘柄数', 1, 10000, value=100, key='search_limit',disabled=not liquidity_enabled)
        enabled = c3.checkbox('購入予算で絞り込む', key='search_budget_enabled')
        budget = c3.number_input('1銘柄の購入上限（円）', min_value=1., value=100000., step=10000., key='search_budget', disabled=not enabled)
        st.caption('売買代金は終値×出来高の概算。ATR14は参考表示。コード指定にも現在の絞り込み条件を適用します。')
    with st.expander('出口・想定費用'):
        c1, c2, c3, c4 = st.columns(4)
        policy = c1.selectbox('出口', list('ABCD'), key='search_exit')
        stop = c2.selectbox('初期損切り（%）', [3.,5.,8.,10.,12.], index=3, key='search_stop')
        commission = c3.number_input('片道手数料（%）', 0., 10., step=.01, key='search_commission')
        slippage = c4.number_input('成行スリッページ（%）', 0., 10., value=.05, step=.01, key='search_slippage')
        st.caption('手数料0%はSBI証券の無料適用を前提とする初期値。スリッページは試算上の仮定。金額は税引前です。')
    params = parameters(strategies)
    return selected_catalog, decision_date(requested, use_today), SearchSettings(
        strategies=strategies, combination=st.session_state.search_mode, minimum_turnover=turnover*1e8,
        liquidity_enabled=liquidity_enabled,
        maximum_candidates=int(limit), budget=budget if enabled else None,
        commission=commission/100, slippage=slippage/100, exit_policy=policy, initial_stop=stop/100, parameters=params)


def fetch_prices(assets, asof, store, reference=False, lookback_years=3):
    tickers = list(assets.ticker)
    if reference and '1306.T' not in tickers:
        tickers.append('1306.T')
    progress = st.progress(0., text='日足を確認しています')
    failures, prices, retrieved = [], {}, 0
    for i, ticker in enumerate(tickers):
        try:
            frame, fetched = store.refresh(ticker, asof,lookback_years=lookback_years)
            prices[ticker] = frame
            if fetched:
                retrieved += 1
                time.sleep(.8)
        except Exception as exc:
            failures.append({'ticker':ticker,'reason':str(exc)})
            prices[ticker] = store.read(ticker)
            if '429' in str(exc) or 'Too Many Requests' in str(exc):
                st.warning('取得元のレート制限で中断しました。保存済みデータを保持しています。')
                break
        progress.progress((i+1)/max(1,len(tickers)), text=f'{i+1:,} / {len(tickers):,}　{ticker}')
    progress.empty()
    if failures:
        st.warning(f'{len(failures)}銘柄の取得に確認事項があります。')
        st.dataframe(pd.DataFrame(failures), hide_index=True)
    return prices, failures, retrieved


def show_results():
    output = st.session_state.get('search_output')
    if not output:
        st.info('条件を選び、検索を実行してください。初回の全市場検索にはデータ準備を使用します。')
        return
    table = pd.DataFrame(output['records'])
    st.subheader('検索結果')
    st.caption(f"判定日 {output['date']}　実行 {output['completed_at']}　取得失敗 {len(output.get('failures',[]))}件")
    actual = output['settings']
    st.caption('実行した入口　'+'、'.join(NAMES[s] for s in actual['strategies'])+'　組合せ '+actual['combination'])
    budget_label = f"{actual['budget']:,.0f}円" if actual['budget'] is not None else 'OFF'
    liquidity_label = f"平均売買代金{actual['minimum_turnover']/1e8:g}億円以上・最大{actual['maximum_candidates']}銘柄" if actual.get('liquidity_enabled',True) else '流動性絞り込みOFF・対象全体'
    st.caption(f"実行条件　{liquidity_label}・予算{budget_label}・出口{actual['exit_policy']}・損切り{actual['initial_stop']:.0%}")
    counts = table.state.value_counts() if not table.empty else pd.Series(dtype=int)
    if st.session_state.get('mobile_view'):
        st.write('　'.join(f'{label} {int(counts.get(label,0))}' for label in ['成立','不成立','判定不可','対象外']))
    else:
        columns = st.columns(4)
        for column, label in zip(columns, ['成立','不成立','判定不可','対象外']):
            column.metric(label, int(counts.get(label,0)))
    states = st.multiselect('表示する状態', ['成立','不成立','判定不可','対象外'], default=['成立'], key='result_states')
    st.session_state['retained_result_states'] = states
    shown = table[table.state.isin(states)] if not table.empty else table
    st.download_button('全結果CSV', table.to_csv(index=False).encode('utf-8-sig'), 'screening.csv', mime='text/csv')
    if shown.empty:
        st.info('選択した状態に該当する銘柄は0件です。状態の表示条件を変更できます。')
        return
    page_count = (len(shown)-1)//20+1
    if st.session_state.get('result_page',1)>page_count:
        st.session_state['result_page'] = 1
    page = st.number_input('結果ページ', 1, page_count, min(st.session_state.get('result_page',1),page_count), key='result_page')
    st.session_state['retained_result_page'] = page
    subset = shown.iloc[(page-1)*20:page*20]
    if st.session_state.get('mobile_view'):
        for row in subset.itertuples():
            with st.container(border=True):
                st.markdown(f'**{row.ticker}　{row.name}**')
                st.write(f'{row.state}　{row.close:,.2f}円' if pd.notna(row.close) else row.state)
                st.write(f'1売買単位の概算購入額 {row.lot_cost:,.0f}円' if pd.notna(row.lot_cost) else '購入額は売買単位を確認待ち')
                st.caption(row.reason)
                if st.button('銘柄詳細', key='open_'+row.ticker):
                    open_detail(row.ticker)
    else:
        names = dict(ticker='コード',name='銘柄名',state='判定',close='株価',lot_size='売買単位',
                     lot_cost='1単位概算額',quantity='予算内株数',cost='概算購入額',turnover='平均売買代金',atr_ratio='ATR比率',reason='判定理由') | NAMES
        if actual['budget'] is None:
            names.pop('quantity')
            names.pop('cost')
        display = subset[[c for c in names if c in subset]].rename(columns=names)
        if 'ATR比率' in display:
            display['ATR14（%）'] = display.pop('ATR比率')*100
        selection_key = f"result_table_{st.session_state.get('selection_generation',0)}_{page}_{','.join(states)}"
        selected = st.dataframe(display, hide_index=True, width='stretch', on_select='rerun', selection_mode='single-row', key=selection_key)
        if selected.selection.rows and selected.selection.rows[0] < len(subset):
            open_detail(subset.iloc[selected.selection.rows[0]].ticker)
        ticker = st.selectbox('詳細を確認する銘柄', list(subset.ticker), format_func=lambda t: t+'　'+str(subset.set_index('ticker').loc[t,'name']))
        if st.button('選択した銘柄を開く'):
            open_detail(ticker)


def open_detail(ticker):
    st.session_state['detail_ticker'] = ticker
    st.session_state['selection_generation'] = st.session_state.get('selection_generation',0)+1
    st.switch_page(st.session_state['detail_page'])


def search_page():
    st.title('銘柄検索')
    st.caption('日足の条件成立を確認し、次の営業日の候補を探す')
    browser_controls()
    with st.expander('営業日規則の更新'):
        st.caption('同梱カレンダーは2014〜2026年に対応しています。年をまたぐときは内閣府の公式祝日を取得して更新できます。')
        if st.button('公式祝日で営業日規則を更新'):
            try:
                info = refresh_calendar()
                st.success('営業日規則を'+info['end']+'まで更新しました。')
            except Exception as exc:
                st.error('更新を完了できませんでした。'+str(exc))
    if st.session_state.pop('reset_today',False):
        st.session_state['use_today'] = False
    try:
        assets, asof, settings = controls()
    except (ValueError, TypeError) as exc:
        st.error(str(exc))
        show_results()
        return
    st.session_state['retained_search_settings'] = saved_settings()
    metadata = json.loads((REFERENCE/'universe.json').read_text(encoding='utf-8'))
    st.caption(f"上場一覧の基準日 {metadata['source_date']}　保存日時 {metadata['frozen_at']}　対象 {len(assets):,}銘柄　実際の判定日 {asof.date()}")
    if asof.date() == pd.Timestamp.now(tz='Asia/Tokyo').date():
        st.warning('今回選択した当日値は暫定値です。配信時点により条件判定が変わります。')
    store = DailyStore()
    lookback_years = required_history_years(settings)
    st.caption(f'指標準備用の取得目安 {lookback_years}年。長い期間に変更した設定は必要な履歴を追加取得します。')
    prepare, run = st.columns(2)
    if prepare.button('全体データを準備・更新', width='stretch', help='現在の対象全体の日足を取得します。取得済みデータを再利用します。'):
        prepared,failures,retrieved = fetch_prices(assets, asof, store, reference=MINERVINI in settings.strategies,lookback_years=lookback_years)
        observed = sum(t in prepared and not prepared[t].empty and asof in prepared[t].index for t in assets.ticker)
        message = f'判定日の価格あり {observed:,}／対象{len(assets):,}銘柄。今回取得 {retrieved:,}銘柄。'
        if failures or observed != len(assets):
            st.warning(message+'取得失敗・未処理の対象は再度準備できます。')
        else:
            st.success(message)
    if run.button('この条件で検索', type='primary', width='stretch'):
        if assets.empty:
            st.error('対象銘柄を指定してください。')
        else:
            cached = {t: store.read(t) for t in assets.ticker}
            missing = [t for t,f in cached.items() if f.empty]
            if st.session_state.search_scope != 'コード指定' and missing:
                st.warning(f'未準備が{len(missing):,}銘柄あります。全体データ準備で取得できます。今回は保存済みの対象を検索します。')
                available = assets[~assets.ticker.isin(missing)]
            else:
                available = assets
            prices, failures, retrieved = fetch_prices(available, asof, store, reference=MINERVINI in settings.strategies,lookback_years=lookback_years)
            with st.spinner('流動性・予算・入口条件を判定しています'):
                calculation = st.progress(0.,text='条件判定を開始します')
                table, details = scan_catalog(assets, prices, asof, settings,
                    progress=lambda value,label:calculation.progress(value,text=label))
                calculation.empty()
            timestamps = {}
            for ticker in prices:
                path = store.root/'prices'/f'{ticker}.json'
                metadata = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
                csv_path = path.with_suffix('.csv')
                matched = csv_path.exists() and metadata.get('price_sha256')==hashlib.sha256(csv_path.read_bytes()).hexdigest()
                timestamps[ticker] = metadata.get('retrieved_at') if matched else None
            table['price_retrieved_at'] = table.ticker.map(timestamps)
            versions = {ticker:price_digest(frame,asof) for ticker,frame in prices.items() if not frame.empty}
            table['price_version'] = table.ticker.map(versions)
            output = dict(records=json.loads(table.to_json(orient='records')), settings=asdict(settings),
                          date=str(asof.date()), completed_at=pd.Timestamp.now(tz='Asia/Tokyo').isoformat(),failures=failures,
                          screening_code_hash=screening_code_hash())
            st.session_state['search_output'] = output
            st.session_state['result_page'] = 1
            st.session_state.pop('result_table',None)
            client = st.session_state.get('browser_client')
            if client:
                write_json(store.root/'results'/f'{client}.json', output)
            st.session_state['reset_today'] = True
            st.rerun()
    previous = st.session_state.get('search_output')
    if previous and (previous['settings'] != asdict(settings) or previous['date'] != str(asof.date())):
        st.info('表示中の結果には前回実行した検索条件を使用しています。変更した条件で確認するときは再検索してください。')
    if previous and previous.get('screening_code_hash') != screening_code_hash():
        st.warning('保存結果を計算した処理の版に変更があります。現在の処理で再検索できます。')
    show_results()


def detail_page():
    st.title('銘柄詳細')
    output = st.session_state.get('search_output')
    ticker = st.session_state.get('detail_ticker')
    if not output or not ticker:
        st.info('銘柄検索の結果から銘柄を選択してください。')
        return
    record = next(r for r in output['records'] if r['ticker']==ticker)
    settings = SearchSettings(**output['settings'])
    if st.button('検索結果へ戻る'):
        st.switch_page(st.session_state['search_page'])
    if output.get('screening_code_hash') != screening_code_hash():
        st.warning('計算処理の版が更新されています。検索結果へ戻り、再検索してください。')
        return
    asof = pd.Timestamp(output['date'])
    raw = DailyStore().read(ticker)
    raw = raw.loc[:asof] if not raw.empty else raw
    if raw.empty or asof not in raw.index:
        st.warning('判定日の価格が不足しています。データを更新して再検索してください。')
        return
    if record.get('price_version') and price_digest(raw,asof) != record['price_version']:
        st.warning('価格データが検索後に更新されています。更新後の価格で再検索してください。')
        return
    if record.get('market_start_date'):
        raw = raw.loc[pd.Timestamp(record['market_start_date']):]
        st.caption('確認済みの市場・上場区間の開始日 '+record['market_start_date'])
        if raw.empty:
            st.info('この上場区間の価格を確認できません。')
            return
    st.subheader(ticker+'　'+record['name'])
    st.caption(f"判定日 {output['date']}　価格取得 {record.get('price_retrieved_at') or '確認待ち'}　売買単位の資料基準 {record.get('lot_known_from') or '確認待ち'}")
    adjusted = indicator_prices(raw, asof)
    chart = adjusted.tail(150)
    figure = go.Figure(go.Candlestick(x=chart.index,open=chart.Open,high=chart.High,low=chart.Low,close=chart.Close,name='調整日足',increasing_line_color='#12856c',decreasing_line_color='#cf5260'))
    figure.update_layout(height=420,xaxis_rangeslider_visible=False,margin=dict(l=10,r=10,t=10,b=10))
    st.plotly_chart(figure,width='stretch')
    st.caption('チャートと指標は調整価格。購入額は判定日の取引価格単位で計算します。気配・売買停止状態・スプレッドは発注前の確認待ちです。')
    benchmark = indicator_prices(DailyStore().read('1306.T'),asof)
    signals, rows = {}, []
    for strategy in settings.strategies:
        try:
            result = evaluate_entry(adjusted,strategy,settings.parameters.get(strategy,{}),benchmark.Close if not benchmark.empty else None)
            signals[strategy] = result
            latest = result.loc[asof]
            rows.append({'入口':NAMES[strategy],'判定':'判定不可' if not latest.ready else '成立' if latest.buy_signal else '不成立'})
            with st.expander(NAMES[strategy]+'の指標と条件'):
                for fields in [('fast','slow','histogram'),('k','d'),('rsi',),('sma50','sma150','sma200')]:
                    available = [name for name in fields if name in result]
                    if available:
                        st.line_chart(result[available].tail(150),width='stretch')
                view = result.tail(30)
                if strategy==LEGACY and settings.parameters.get(strategy,{}).get('price_sma',16)!=16:
                    view = view.rename(columns={'sma16':'株価SMA（設定値）'})
                st.dataframe(view,width='stretch')
        except (ValueError,KeyError,TypeError) as exc:
            rows.append({'入口':NAMES[strategy],'判定':'判定不可','理由':str(exc)})
    st.dataframe(pd.DataFrame(rows),hide_index=True,width='stretch')
    st.subheader('購入と出口の参考水準')
    historical = st.radio('計算する想定',['これから購入する想定','過去日の寄付に購入した想定'],horizontal=True,key='projection_mode_'+ticker) == '過去日の寄付に購入した想定'
    c1,c2,c3 = st.columns(3)
    entry_date = c2.date_input('想定購入日',asof.date(),min_value=raw.index[0].date(),max_value=asof.date(),key='entry_date_'+ticker) if historical else asof.date()
    if pd.Timestamp(entry_date) not in raw.index:
        st.info('価格を確認できる営業日を選択してください。')
        return
    base = raw.loc[str(entry_date),'Open'] if historical else raw.loc[asof,'Close']
    entry = c1.number_input('想定購入約定価格（円）', min_value=.01,value=float(base*(1+settings.slippage)),key=f'entry_{ticker}_{entry_date}_{historical}')
    lot = record['lot_size']
    if not lot:
        st.info('ETFの売買単位は確認待ちです。公式資料で確認した値を入力すると参考額を計算できます。')
        lot = c3.number_input('確認した売買単位',0,100000,0,key='manual_lot_'+ticker)
        if not lot:
            return
    estimate = purchase_estimate(entry,lot,settings.budget,settings.commission,0)
    if estimate['quantity'] == 0:
        st.warning('現在の購入予算では1売買単位を購入できません。以下は指定した株数による参考計算です。')
    quantity = c3.number_input('想定購入株数・口数',min_value=int(lot),value=max(int(lot),estimate['quantity']),step=int(lot),key='quantity_'+ticker)
    if quantity % int(lot):
        st.error('売買単位の整数倍で入力してください。')
        return
    if historical:
        from .screening_workspace import sessions
        path = raw.reindex(sessions(entry_date,asof))
    else:
        path = raw.iloc[:0]
    if 'Stock Splits' in path and path['Stock Splits'].fillna(0).ne(0).any():
        st.warning('購入日以後に分割・併合があります。想定購入価格の単位を現在の価格単位に合わせて確認してください。')
    for strategy in settings.strategies:
        position,pnl,projection = exit_projection(entry,path.Close,path.High if settings.exit_policy=='B' and strategy==MINERVINI else None,settings.exit_policy,settings.initial_stop,quantity,settings.commission,settings.slippage,
            ohlc=path if historical else None,signals=signals.get(strategy),strategy_id=strategy)
        st.markdown('**'+NAMES[strategy]+'　出口'+settings.exit_policy+'**')
        a,b,c = st.columns(3)
        a.metric('決済時のストップ' if position.get('closed') else '翌営業日用ストップ',f"{position['stop']:,.2f}円")
        b.metric('利確水準',f"{position['target']:,.2f}円" if position.get('target') is not None else '設定なし')
        c.metric('参考決済の純損益' if position.get('closed') else 'ストップ約定時の想定純損益',f'{pnl:+,.0f}円')
        if position.get('closed'):
            st.caption(f"参考決済日 {position['exit_date']}　理由 {position['exit_reason']}　参考約定価格 {position['exit_price']:,.2f}円")
        if position.get('path_missing'):
            st.warning('途中の価格・出来高に欠損等があり、価格到達の順序を確認できない区間を含む参考計算です。')
        if settings.exit_policy=='B':
            st.caption('手法別出口　'+('観測高値から10%トレーリング' if strategy==MINERVINI else 'RSI50以上で翌営業日寄付の売却条件' if strategy in (RSI2,RSI14) else '売りシグナル成立で翌営業日寄付の売却条件'))
        if not projection.empty:
            st.dataframe(projection,hide_index=True,width='stretch')
    st.caption(f'片道手数料 {settings.commission:.2%}、成行スリッページ {settings.slippage:.2%}。税引前の仮想計算。ギャップ・急変では想定損失を超える場合があります。C/Dの基準幅は8%、初期利確は24%。')


@st.cache_data(show_spinner=False)
def research_data(bundle, version):
    root = REFERENCE/'research'/bundle
    metadata = json.loads(version)
    for name,expected in metadata['output_hashes'].items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:
            raise ValueError('同梱した検証結果のハッシュが不一致です')
    return pd.read_csv(root/'summary.csv'),pd.read_csv(root/'curves.csv.gz',parse_dates=['date'])


def research_page():
    st.title('検証結果')
    active = REFERENCE/'research/active.json'
    if not active.exists():
        st.info('検証結果データの準備待ちです。')
        return
    pointer = json.loads(active.read_text(encoding='utf-8'))
    bundle = pointer['bundle']
    if not re.fullmatch(r'[a-f0-9]{20}',bundle):
        st.error('検証結果の保存版を確認できません。')
        return
    source = REFERENCE/'research'/bundle/'source.json'
    if pointer.get('manifest_sha256') and hashlib.sha256(source.read_bytes()).hexdigest()!=pointer['manifest_sha256']:
        st.error('検証結果の取得元記録と公開版のハッシュが一致しません。')
        return
    metadata = json.loads(source.read_text(encoding='utf-8'))
    summary,curves = research_data(bundle,source.read_text(encoding='utf-8'))
    st.caption(f"初期資金1,000万円・費用込み10万円枠・仮想端数株。片道手数料{metadata['commission']:.2%}、成行スリッページ{metadata['slippage']:.2%}、税引前。")
    if metadata.get('prepared_count'):
        st.caption(f"検証の固定対象 {metadata['prepared_count']:,}銘柄。検索用の上場一覧は{len(catalog()):,}銘柄で、採用範囲に差があります。")
    st.info('参照成績は固定上場一覧による検証です。現在の流動性・予算・実売買単位、入口の数値変更、複数入口のOR／ANDを追加した設定は未検証です。')
    current = st.session_state.get('search_commission',0)/100
    if not np.isclose(current,metadata['commission']):
        st.warning(f'画面の手数料設定{current:.2%}と参照成績{metadata["commission"]:.2%}に差があります。')
    current_slippage = st.session_state.get('search_slippage',.05)/100
    if not np.isclose(current_slippage,metadata['slippage']):
        st.warning(f'画面のスリッページ設定{current_slippage:.2%}と参照成績{metadata["slippage"]:.2%}に差があります。')
    c1,c2 = st.columns(2)
    period = c1.selectbox('検証期間',['2021_2025','2016_2020'],format_func=lambda s:s.replace('_','〜'))
    group = c2.selectbox('資産区分',['stock','etf'],format_func=lambda s:'個別株' if s=='stock' else 'ETF')
    strategies = st.multiselect('参照する入口',STRATEGIES,default=st.session_state.get('search_strategies',[TV]),format_func=NAMES.get,key='research_strategies')
    selected = summary[summary.period.eq(period)&summary.asset_class.eq(group)&summary.strategy_id.isin(strategies)].sort_values('final_nav',ascending=False).copy()
    if selected.empty:
        st.info('入口を選択してください。')
        return
    selected['入口'] = selected.strategy_id.map(NAMES)
    selected['順位'] = selected.final_nav.rank(method='min',ascending=False).astype(int)
    fields = {'順位':'順位','入口':'入口','exit_policy':'出口','initial_stop':'初期損切り（%）','final_nav':'期末資産（円）','cagr':'年率（%）','max_drawdown':'最大下落率（%）','trade_count':'決済件数','average_net_pnl':'1取引平均純損益（円）','open_positions':'期末保有数'}
    display = selected[list(fields)].rename(columns=fields)
    for column in ['初期損切り（%）','年率（%）','最大下落率（%）']:
        display[column] *= 100
    if period=='2021_2025':
        fund_return = float(selected.benchmark_total_return.iloc[0])
        st.metric('オルカン同額保有の期末資産',f'{10_000_000*(1+fund_return):,.0f}円',f'{fund_return:+.2%}')
    else:
        st.caption('オルカンは設定前を含むため、この全期間との比較は不可。')
    st.dataframe(display,hide_index=True,width='stretch')
    st.download_button('表示条件の成績CSV',selected.to_csv(index=False).encode('utf-8-sig'),'research.csv')
    labels = {r.case_id:f'{NAMES[r.strategy_id]}・出口{r.exit_policy}・損切り{r.initial_stop:.0%}' for r in selected.itertuples()}
    case = st.selectbox('資産推移を表示する条件',selected.case_id.tolist(),format_func=labels.get)
    curve = curves[curves.case_id.eq(case)].set_index('date')
    st.line_chart(curve[['strategy_nav','all_country_nav']].rename(columns={'strategy_nav':'戦略純資産','all_country_nav':'オルカン'}),width='stretch')
    row = selected[selected.case_id.eq(case)].iloc[0]
    st.caption(f'1取引平均純損益は決済済み{int(row.trade_count)}件の標本平均。期末資産には含み損益を含みます。')


def render_workspace():
    st.set_page_config(page_title='日本株スクリーナー',layout='wide',initial_sidebar_state='collapsed')
    for key,value in st.session_state.get('retained_search_settings',{}).items():
        if key not in st.session_state:
            st.session_state[key] = pd.Timestamp(value).date() if key=='requested_date' else value
    for key in ('result_states','result_page'):
        if key not in st.session_state and 'retained_'+key in st.session_state:
            st.session_state[key] = st.session_state['retained_'+key]
    st.markdown('''<style>
    .stApp{background:#f6f8fb;color:#172b45;font-family:Inter,"Yu Gothic",sans-serif}
    h1,h2,h3{color:#172b45;letter-spacing:-.03em}
    .block-container{max-width:1440px;padding-top:2rem;padding-bottom:3rem}
    [data-testid="stMetric"]{background:white;border:1px solid #dce3ec;border-radius:10px;padding:14px}
    [data-testid="stExpander"]{background:#fff;border-color:#dce3ec}
    @media(max-width:760px){.block-container{padding:1rem}h1{font-size:1.8rem}}
    </style>''',unsafe_allow_html=True)
    search = st.Page(search_page,title='銘柄検索',icon=':material/search:',default=True)
    detail = st.Page(detail_page,title='銘柄詳細',icon=':material/candlestick_chart:')
    research = st.Page(research_page,title='検証結果',icon=':material/query_stats:')
    st.session_state['search_page'],st.session_state['detail_page'] = search,detail
    st.navigation([search,detail,research],position='top').run()
