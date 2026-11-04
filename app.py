import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, timedelta

# ページ設定
st.set_page_config(
    page_title="リンダ・ラシュキ MACD 銘柄スクリーナー",
    page_icon="📈",
    layout="wide"
)

# 銘柄プリセットリストの定義
PRESET_TICKERS = {
    "日本株（日経225主要・代表銘柄）": [
        "7203.T", "9984.T", "6758.T", "8306.T", "6861.T", "6098.T", "8035.T", "9983.T", "7751.T", "4502.T",
        "6501.T", "7267.T", "8316.T", "8058.T", "8031.T", "6367.T", "4063.T", "6954.T", "7974.T", "3382.T",
        "6981.T", "4519.T", "6702.T", "7733.T", "8001.T", "8002.T", "2502.T", "2503.T", "4901.T", "6902.T",
        "8411.T", "8766.T", "8801.T", "8802.T", "9020.T", "9022.T", "9201.T", "9202.T", "9432.T", "9433.T",
        "9434.T", "9501.T", "9502.T", "9503.T", "9101.T", "9104.T", "9107.T", "7011.T", "7012.T", "6503.T"
    ],
    "米国株（主要大型株・ハイテク）": [
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B", "JNJ", "V",
        "WMT", "JPM", "PG", "MA", "UNH", "HD", "DIS", "BAC", "PFE", "XOM",
        "CVX", "KO", "PEP", "CSCO", "INTC", "AMD", "NFLX", "CRM", "ABT", "ORCL"
    ],
    "FX (主要通貨ペア)": [
        "USDJPY=X", "EURJPY=X", "GBPJPY=X", "AUDJPY=X", "EURUSD=X", "GBPUSD=X", "AUDUSD=X", "USDCAD=X", "USDCHF=X"
    ]
}

def calculate_linda_macd(df):
    """
    リンダ・ラシュキの 3/10/16 MACD (3/10 Oscillator) を計算
    - 3日EMAと10日EMAの差 (3/10 Oscillator)
    - その16日SMA (Signal)
    - ヒストグラム (Oscillator - Signal)
    - 16日SMA (株価本体の16SMA Trend)
    """
    df = df.copy()
    close = df['Close']
    
    # 3日EMAと10日EMA
    ema3 = close.ewm(span=3, adjust=False).mean()
    ema10 = close.ewm(span=3, adjust=False).mean() # 注: 3/10 Oscillator
    # ※ 一般的な3/10オシレーター: 3日EMA - 10日EMA
    # yfinance / TAの標準表現:
    ema3_real = close.ewm(span=3, adjust=False).mean()
    ema10_real = close.ewm(span=10, adjust=False).mean()
    
    df['3/10_Oscillator'] = ema3_real - ema10_real
    df['3/10_Signal'] = df['3/10_Oscillator'].rolling(window=16).mean()
    df['3/10_Hist'] = df['3/10_Oscillator'] - df['3/10_Signal']
    
    # 株価本体の16日Simple Moving Average
    df['SMA16'] = close.rolling(window=16).mean()
    
    return df

def check_conditions(df):
    """
    直近データにおける3/10 Oscillator及びAnti手法の条件判定
    """
    if len(df) < 30:
        return None
    
    latest = df.iloc[-1]
    prev1 = df.iloc[-2]
    prev2 = df.iloc[-3]
    
    osc = df['3/10_Oscillator']
    sig = df['3/10_Signal']
    hist = df['3/10_Hist']
    close = df['Close']
    sma16 = df['SMA16']
    
    # 1. 3/10 MACD Golden Cross (直近でゴールデンクロス)
    gc_today = (prev1['3/10_Oscillator'] <= prev1['3/10_Signal']) and (latest['3/10_Oscillator'] > latest['3/10_Signal'])
    # 2. 3/10 MACD Dead Cross (直近でデッドクロス)
    dc_today = (prev1['3/10_Oscillator'] >= prev1['3/10_Signal']) and (latest['3/10_Oscillator'] < latest['3/10_Signal'])
    
    # 3. Anti (Buy Signal):
    # - 全体トレンドが上昇方向（またはオシレーターが0以上 / SMA16が右肩上がり）
    # - オシレーターが一時的に低下（プルバック）した後、上昇へ反転するポイント（Histフック）
    anti_buy = False
    if (prev2['3/10_Hist'] > prev1['3/10_Hist']) and (latest['3/10_Hist'] > prev1['3/10_Hist']):
        # オシレーターの傾きが反転（V字）
        if latest['Close'] > latest['SMA16']: # 上昇トレンド中の押し目
            anti_buy = True

    # 4. Anti (Sell Signal):
    # - トレンドが下落方向で、オシレーターが一時的に上昇した後、下降へ反転（フック）
    anti_sell = False
    if (prev2['3/10_Hist'] < prev1['3/10_Hist']) and (latest['3/10_Hist'] < prev1['3/10_Hist']):
        if latest['Close'] < latest['SMA16']: # 下降トレンド中の戻り
            anti_sell = True
            
    return {
        "Close": round(latest['Close'], 2),
        "SMA16": round(latest['SMA16'], 2),
        "3/10_Osc": round(latest['3/10_Oscillator'], 3),
        "3/10_Sig": round(latest['3/10_Signal'], 3),
        "3/10_Hist": round(latest['3/10_Hist'], 3),
        "3/10_GC": gc_today,
        "3/10_DC": dc_today,
        "Anti_Buy": anti_buy,
        "Anti_Sell": anti_sell
    }

def main():
    st.title("📈 リンダ・ラシュキ MACD 銘柄スクリーナー")
    st.markdown("""
    リンダ・ラシュキ（Linda Raschke）氏の **3/10 Oscillator (3/10/16 MACD)** および **Anti（アンチ）手法** に基づいて銘柄をスクリーニングします。
    """)

    st.sidebar.header("🔍 スクリーニング設定")
    
    market_choice = st.sidebar.selectbox(
        "対象市場を選択",
        options=list(PRESET_TICKERS.keys()) + ["カスタム（ティッカー指定）"]
    )
    
    if market_choice == "カスタム（ティッカー指定）":
        custom_input = st.sidebar.text_area("ティッカーシンボル（カンマ区切り）", value="7203.T, 9984.T, AAPL, MSFT, USDJPY=X")
        ticker_list = [t.strip().upper() for t in custom_input.split(",") if t.strip()]
    else:
        ticker_list = PRESET_TICKERS[market_choice]
        
    st.sidebar.markdown("---")
    st.sidebar.subheader("🎯 条件フィルタ")
    filter_gc = st.sidebar.checkbox("3/10 MACD ゴールデンクロス (本日/直近)", value=False)
    filter_dc = st.sidebar.checkbox("3/10 MACD デッドクロス (本日/直近)", value=False)
    filter_anti_buy = st.sidebar.checkbox("Anti 買いシグナル (押し目買い)", value=True)
    filter_anti_sell = st.sidebar.checkbox("Anti 売りシグナル (戻り売り)", value=False)

    period_choice = st.sidebar.selectbox("データ取得期間", options=["3mo", "6mo", "1y", "2y"], index=1)

    if st.button("🚀 スクリーニング実行", type="primary"):
        results = []
        progress_bar = st.progress(0)
        status_text = st.empty()

        data_dict = {}

        for i, ticker in enumerate(ticker_list):
            status_text.text(f"データ取得中... ({i+1}/{len(ticker_list)}): {ticker}")
            try:
                stock = yf.Ticker(ticker)
                df = stock.history(period=period_choice)
                if not df.empty and len(df) >= 30:
                    df = calculate_linda_macd(df)
                    data_dict[ticker] = df
                    res = check_conditions(df)
                    if res:
                        res["Ticker"] = ticker
                        # フィルタリング判定
                        match = True
                        if filter_gc and not res["3/10_GC"]:
                            match = False
                        if filter_dc and not res["3/10_DC"]:
                            match = False
                        if filter_anti_buy and not res["Anti_Buy"]:
                            match = False
                        if filter_anti_sell and not res["Anti_Sell"]:
                            match = False
                        
                        if match:
                            results.append(res)
            except Exception as e:
                pass
            
            progress_bar.progress((i + 1) / len(ticker_list))

        status_text.text("スクリーニング完了！")
        
        st.session_state['screening_results'] = results
        st.session_state['data_dict'] = data_dict

    # 結果表示
    if 'screening_results' in st.session_state:
        results = st.session_state['screening_results']
        data_dict = st.session_state.get('data_dict', {})
        
        st.subheader(f"📊 該当銘柄リスト（全 {len(results)} 件）")
        
        if results:
            res_df = pd.DataFrame(results)
            cols_order = ["Ticker", "Close", "SMA16", "3/10_Osc", "3/10_Sig", "3/10_Hist", "3/10_GC", "3/10_DC", "Anti_Buy", "Anti_Sell"]
            res_df = res_df[cols_order]
            
            st.dataframe(res_df, use_container_width=True)
            
            # CSVダウンロードボタン
            csv_data = res_df.to_csv(index=False).encode('utf-8-sig')
            st.download_button(
                label="📥 検索結果をCSVダウンロード",
                data=csv_data,
                file_name=f"linda_macd_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv"
            )
            
            st.markdown("---")
            st.subheader("📈 チャート詳細閲覧")
            selected_ticker = st.selectbox("詳細チャートを表示する銘柄を選択", options=[r["Ticker"] for r in results])
            
            if selected_ticker in data_dict:
                df_chart = data_dict[selected_ticker]
                
                # Plotlyによるインタラクティブチャート
                fig = make_subplots(
                    rows=2, cols=1,
                    shared_xaxes=True,
                    vertical_spacing=0.05,
                    row_heights=[0.6, 0.4],
                    subplot_titles=(f"{selected_ticker} 株価 & 16SMA", "3/10 Oscillator & Signal")
                )

                # ローソク足
                fig.add_trace(go.Candlestick(
                    x=df_chart.index,
                    open=df_chart['Open'],
                    high=df_chart['High'],
                    low=df_chart['Low'],
                    close=df_chart['Close'],
                    name="株価"
                ), row=1, col=1)

                # 16SMA
                fig.add_trace(go.Scatter(
                    x=df_chart.index,
                    y=df_chart['SMA16'],
                    mode='lines',
                    name='16 SMA',
                    line=dict(color='orange', width=1.5)
                ), row=1, col=1)

                # 3/10 Oscillator
                fig.add_trace(go.Scatter(
                    x=df_chart.index,
                    y=df_chart['3/10_Oscillator'],
                    mode='lines',
                    name='3/10 Osc',
                    line=dict(color='blue', width=1.5)
                ), row=2, col=1)

                # 16 Signal
                fig.add_trace(go.Scatter(
                    x=df_chart.index,
                    y=df_chart['3/10_Signal'],
                    mode='lines',
                    name='16 Signal',
                    line=dict(color='red', width=1.5)
                ), row=2, col=1)

                # Hist (Bar)
                colors = ['red' if val < 0 else 'green' for val in df_chart['3/10_Hist']]
                fig.add_trace(go.Bar(
                    x=df_chart.index,
                    y=df_chart['3/10_Hist'],
                    name='Hist',
                    marker_color=colors,
                    opacity=0.4
                ), row=2, col=1)

                fig.update_layout(
                    xaxis_rangeslider_visible=False,
                    height=650,
                    margin=dict(l=20, r=20, t=40, b=20)
                )

                st.plotly_chart(fig, use_container_width=True)

        else:
            st.info("条件に一致する銘柄は見つかりませんでした。条件を緩和するか、対象市場・期間を変更して再検索してください。")

if __name__ == "__main__":
    main()
