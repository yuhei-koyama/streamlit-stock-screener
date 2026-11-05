"""Shared version-page presentation; all indicator decisions live in the core."""
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from app import fetch_daily_prices, exclude_current_day_bar, normalize_tickers
from .linda_versions import TV, LEGACY, ANTI_310, ANTI_STOCH, VERSIONS, TV_DEFAULTS, evaluate_version
from .universe import MAX_TICKERS, PRESET_TICKERS

DESCRIPTIONS = {
    TV: '添付Pine版：EMA3−EMA10とEMA16のクロス ＋ ストキャス7/3/10のHookが同じ足で成立。リンダ原典そのものではありません。',
    ANTI_310: '既存のSMA3/10/16適応版。SetupとHookを区別します。Hookは価格によるエントリー確定ではありません。',
    ANTI_STOCH: '既存のストキャス7/4/10版。押し3本、交差許可。原典の完全再現を検証済みという意味ではありません。',
    LEGACY: '初期コミット8f7aa99の試作判定：EMA3−EMA10、SignalはSMA16。ヒストグラム反転と株価SMA16で候補を判定。原典Antiとは別の比較用試作です。',
}


def build_version_chart(prices, result, strategy_id):
    figure = make_subplots(rows=3 if strategy_id == TV else 2, cols=1, shared_xaxes=True)
    figure.add_trace(go.Scatter(x=prices.index, y=prices.Close, name='調整済み終値'), row=1, col=1)
    if 'sma16' in result:
        figure.add_trace(go.Scatter(x=result.index, y=result.sma16, name='価格SMA16'), row=1, col=1)
    for name, symbol, color in [('buy_signal', 'triangle-up', 'green'), ('sell_signal', 'triangle-down', 'red')]:
        mask = result[name].fillna(False)
        figure.add_trace(go.Scatter(x=result.index[mask], y=prices.Close.reindex(result.index[mask]),
                                   name=name, mode='markers', marker=dict(symbol=symbol, color=color, size=10)), row=1, col=1)
    for name in ('fast', 'slow', 'histogram'):
        figure.add_trace(go.Scatter(x=result.index, y=result[name], name=name), row=2, col=1)
    if strategy_id == TV:
        for name in ('k', 'd'):
            figure.add_trace(go.Scatter(x=result.index, y=result[name], name=name), row=3, col=1)
    figure.update_layout(height=750 if strategy_id == TV else 600, margin=dict(l=10, r=10, b=10, t=35))
    return figure


def render_version_page(strategy_id):
    title = VERSIONS[strategy_id][0]
    is_anti = strategy_id in (ANTI_310, ANTI_STOCH)
    st.set_page_config(page_title=title, layout='wide')
    st.title(title)
    st.caption(DESCRIPTIONS[strategy_id])
    st.code(f'{strategy_id} / definition 1.0 / {VERSIONS[strategy_id][1]}', language=None)
    if strategy_id == TV:
        st.info('添付コードの計算式・条件を移植した版です。TradingView実データとの発生日一致は未検証です。データ供給元、価格調整、履歴開始点、欠損処理により差が生じ得ます。')
    st.caption('BUY/SELLは各版の候補名です。SELLを保有株の決済・新規空売りのどちらに使うかは未定義。売買注文・損益計算は行いません。')
    universe = st.selectbox('検索対象', [*PRESET_TICKERS, 'カスタム'])
    defaults = ', '.join(PRESET_TICKERS[universe]) if universe in PRESET_TICKERS else '7203.T, 1306.T'
    raw = st.text_area('東証コード（編集可能・最大100銘柄）', defaults, key=f'{strategy_id}_codes_{universe}')
    period = st.selectbox('取得期間', ['6mo', '1y', '2y', '5y'], index=1)
    drop_today = st.checkbox('当日足を除外', True)
    st.caption('除外ONでは引け後も当日足を除外します。OFFでは場中の未確定足を含み得ます。市場終了・配信完了の自動判定は未実装です。')
    parameters = {}
    if strategy_id == TV:
        with st.expander('Pine期間パラメータ'):
            for name, default in TV_DEFAULTS.items():
                labels = dict(fast_len='MACD Fast EMA期間', slow_len='MACD Slow EMA期間', signal_len='MACD Signal EMA期間', k_len='ストキャスRawK期間', d_len='ストキャスD平滑期間', smooth_k='ストキャスK平滑期間')
                parameters[name] = int(st.number_input(labels[name], min_value=1, value=default, step=1))
    elif strategy_id == ANTI_310:
        parameters['pullback_bars'] = st.radio('押し期間', [3, 2], horizontal=True)
        parameters['allow_cross'] = st.checkbox('Slow通過を許可', True)
    filters = ['最新BUY候補のみ', '最新SELL候補のみ', '最新BUY/SELL候補', '全件（準備・不成立・データ不足を含む）']
    if is_anti:
        filters.append('最新Setupのみ')
        st.caption('このページのBUY/SELL候補はHook Long/Shortです。Setupは準備状態です。')
    selected_filter = st.selectbox('表示条件', filters, index=2)
    key = f'linda_version_result_{strategy_id}'
    if st.button('検索実行', type='primary'):
        tickers, invalid = normalize_tickers(raw)
        if invalid or not 1 <= len(tickers) <= MAX_TICKERS:
            st.error('有効な東証コードを1〜100銘柄指定してください。')
        else:
            results, errors = {}, []
            progress = st.progress(0.0)
            for position, ticker in enumerate(tickers, 1):
                try:
                    prices = exclude_current_day_bar(fetch_daily_prices(ticker, period), drop_today)
                    if prices.empty:
                        raise ValueError('判定対象の足がありません')
                    result = evaluate_version(prices, strategy_id, **parameters)
                    results[ticker] = (prices, result)
                except Exception as exc:
                    errors.append(f'{ticker}: {type(exc).__name__}: {exc}')
                progress.progress(position / len(tickers), text=f'{position}/{len(tickers)}: {ticker}')
            snapshot = dict(strategy_id=strategy_id, parameters=parameters, period=period,
                            drop_today=drop_today, tickers=tickers,
                            run_at=pd.Timestamp.now(tz='Asia/Tokyo').isoformat())
            st.session_state[key] = (results, errors, snapshot)
    if key not in st.session_state:
        st.info('設定を確認して検索実行を押してください。起動時には株価を取得しません。')
        return
    results, errors, snapshot = st.session_state[key]
    st.caption('保存済み結果の実行設定（変更後は再検索してください）')
    st.json(snapshot, expanded=False)
    st.caption('実行時刻はYahooデータの更新時刻ではありません。価格取得は最大15分キャッシュされます。判定対象日も確認してください。')
    for error in errors:
        st.error(error)
    summaries = []
    for ticker, (_, result) in results.items():
        latest = result.iloc[-1]
        setup_long = bool(latest.get('setup_long', False))
        setup_short = bool(latest.get('setup_short', False))
        summaries.append(dict(Ticker=ticker, 判定日=str(result.index[-1]),
                              BUY候補=bool(latest.buy_signal), SELL候補=bool(latest.sell_signal),
                              Setup_Long=setup_long if is_anti else None, Setup_Short=setup_short if is_anti else None,
                              状態='データ不足' if not latest.ready else latest.get('phase', '判定可能'),
                              Strategy=strategy_id, Definition=latest.definition_version,
                              Parameters=latest.parameter_set))
    table = pd.DataFrame(summaries)
    if not table.empty:
        if selected_filter == filters[0]:
            table = table[table['BUY候補']]
        elif selected_filter == filters[1]:
            table = table[table['SELL候補']]
        elif selected_filter == filters[2]:
            table = table[table['BUY候補'] | table['SELL候補']]
        elif selected_filter == '最新Setupのみ':
            table = table[table.Setup_Long | table.Setup_Short]
    st.subheader(f'最新足の候補一覧（{len(table)}件）')
    st.dataframe(table, hide_index=True)
    if results:
        ticker = st.selectbox('詳細・過去のシグナル発生日', list(results))
        prices, result = results[ticker]
        st.plotly_chart(build_version_chart(prices, result, strategy_id), use_container_width=True)
        st.subheader('過去の候補発生日（売買履歴ではありません）')
        event_columns = ['decision_timestamp', 'buy_signal', 'sell_signal', 'strategy_id', 'parameter_set']
        st.dataframe(result.loc[result.buy_signal | result.sell_signal, event_columns], hide_index=True)
        with st.expander('各足の判定と中間値'):
            st.dataframe(result.tail(50))
        st.download_button('各足の判定CSV', result.to_csv().encode('utf-8-sig'), f'{strategy_id}_{ticker}.csv')
