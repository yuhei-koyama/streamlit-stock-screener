"""Additional screening UI; calculations live in the shared core."""
import pandas as pd
import streamlit as st
from app import normalize_tickers, fetch_daily_prices, exclude_current_day_bar
from stock_screener.search import search_rsi, search_minervini
from stock_screener.universe import MAX_TICKERS, PRESET_TICKERS

st.set_page_config(page_title='追加銘柄検索', layout='wide')
st.title('ミネルヴィニ系・RSI 銘柄検索')
st.caption('検索条件の検出のみ。売買指示・出口判定・バックテスト・収益性評価は行いません。Anti検索は既存のトップページを利用してください。')
strategy = st.selectbox('検索条件', ['ミネルヴィニ派生版', 'RSI平均回帰'])
universe = st.selectbox('検索対象', [*PRESET_TICKERS, 'カスタム'])
default_codes = ', '.join(PRESET_TICKERS[universe]) if universe in PRESET_TICKERS else '7203, 6758, 1306, 1321'
raw = st.text_area(f'東証コード（最大{MAX_TICKERS}銘柄・編集可能）', default_codes, key=f'codes_{universe}')
selected_tickers, invalid_codes = normalize_tickers(raw)
st.caption(f'検索対象: {len(selected_tickers)}銘柄。候補一覧は手動管理の代表例で、東証全銘柄ではありません。')
benchmark_raw = st.text_input('RS比較対象（1306.TはTOPIX連動ETFの代理系列）', '1306.T')
period = st.selectbox('取得期間', ['2y', '5y'])
rsi_period = st.selectbox('RSI期間', [2, 14])
threshold = st.number_input('RSI閾値（未満を検索）', 0., 100., 10. if rsi_period == 2 else 30., key=f'threshold_{rsi_period}')
trend = st.checkbox('RSI検索で終値 > SMA200を要求', True)
matched_only = st.checkbox('成立銘柄のみ表示', True)
if st.button('検索実行'):
    tickers, invalid = normalize_tickers(raw)
    benchmark_tickers, bad = normalize_tickers(benchmark_raw)
    if invalid or not 1 <= len(tickers) <= MAX_TICKERS or (strategy == 'ミネルヴィニ派生版' and (bad or len(benchmark_tickers) != 1)):
        st.error(f'対象は有効な東証コード1〜{MAX_TICKERS}銘柄、RS比較対象は1銘柄で指定してください。')
    else:
        summaries, details = [], {}
        try:
            benchmark = None
            if strategy == 'ミネルヴィニ派生版':
                benchmark = exclude_current_day_bar(fetch_daily_prices(benchmark_tickers[0], period), True)
                if benchmark.empty:
                    raise ValueError('RS比較対象のデータがありません')
            progress = st.progress(0.0)
            status = st.empty()
            for position, ticker in enumerate(tickers, start=1):
                status.text(f'取得・判定中 ({position}/{len(tickers)}): {ticker}')
                try:
                    prices = exclude_current_day_bar(fetch_daily_prices(ticker, period), True)
                    if prices.empty:
                        raise ValueError('日足データがありません')
                    result = (search_minervini(prices, benchmark.Close) if benchmark is not None
                              else search_rsi(prices, rsi_period, threshold, trend))
                    latest = result.iloc[-1]
                    summaries.append({'Ticker': ticker, '判定日': str(result.index[-1]), '成立': bool(latest['match']), '状態': latest.reason})
                    details[ticker] = result
                except Exception as exc:
                    st.error(f'{ticker}: {type(exc).__name__}: {exc}')
                progress.progress(position / len(tickers))
            status.text('検索完了')
            st.session_state['search_output'] = (summaries, details, strategy)
        except Exception as exc:
            st.error(f'取得失敗: {type(exc).__name__}: {exc}')
            st.session_state.pop('search_output', None)
if 'search_output' in st.session_state:
    summaries, details, used_strategy = st.session_state['search_output']
    st.caption(f'直近の実行条件: {used_strategy}。当日足を除外。条件変更後は再検索してください。')
    table = pd.DataFrame(summaries)
    st.dataframe(table[table['成立']] if matched_only and not table.empty else table, hide_index=True)
    if details:
        ticker = st.selectbox('条件別合否・中間値・価格', list(details))
        st.line_chart(details[ticker][['Close']])
        st.dataframe(details[ticker].tail(30))
        st.download_button('中間値CSV', details[ticker].to_csv().encode('utf-8-sig'), 'search.csv')
st.info('RSは126本リターン比の独自定義です。専有RS Ratingではありません。成立は収益性を保証しません。')
