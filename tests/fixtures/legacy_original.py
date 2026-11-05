"""Unmodified calculation/check functions from git 8f7aa99:app.py."""
import pandas as pd
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

