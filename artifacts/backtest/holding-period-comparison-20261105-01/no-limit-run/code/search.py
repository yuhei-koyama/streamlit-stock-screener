"""Search conditions only: no orders, exits or performance evaluation."""
import numpy as np
import pandas as pd
from .validation import validate_market_data


def rsi(close, period):
    """Wilder RSI with an initial period-length arithmetic mean; gaps reset."""
    if period not in (2, 14):
        raise ValueError('RSI period must be 2 or 14')
    values = pd.Series(np.nan, index=close.index)
    gains, losses = [], []
    previous = None
    avg_gain = avg_loss = None
    for i, value in enumerate(close):
        if not np.isfinite(value):
            previous = None
            gains, losses = [], []
            avg_gain = avg_loss = None
            continue
        if previous is not None:
            change = value - previous
            gain, loss = max(change, 0), max(-change, 0)
            if avg_gain is None:
                gains.append(gain)
                losses.append(loss)
                if len(gains) == period:
                    avg_gain, avg_loss = np.mean(gains), np.mean(losses)
            else:
                avg_gain = (avg_gain * (period - 1) + gain) / period
                avg_loss = (avg_loss * (period - 1) + loss) / period
            if avg_gain is not None:
                values.iloc[i] = (50 if avg_gain == avg_loss == 0 else
                                  100 if avg_loss == 0 else
                                  100 - 100 / (1 + avg_gain / avg_loss))
        previous = value
    return values


def search_rsi(frame, period=2, threshold=None, trend_filter=True):
    out = validate_market_data(frame, ('Close',))
    threshold = (10 if period == 2 else 30) if threshold is None else threshold
    if not 0 <= threshold <= 100:
        raise ValueError('RSI threshold must be between 0 and 100')
    out['rsi'] = rsi(out.Close.astype(float), period)
    out['sma200'] = out.Close.rolling(200).mean()
    out['ready'] = out.rsi.notna() & (out.sma200.notna() if trend_filter else True)
    out['oversold'] = out.rsi < threshold
    out['trend_pass'] = out.Close > out.sma200 if trend_filter else True
    out['match'] = out.ready & out.oversold & out.trend_pass
    out['reason'] = np.where(~out.ready, 'insufficient_data', np.where(out.match, 'match', 'conditions_not_met'))
    return out


def search_minervini(frame, benchmark):
    """Project-specific template + RS + volume breakout, not proprietary RS Rating."""
    out = validate_market_data(frame, ('High', 'Low', 'Close', 'Volume'))
    for n in (50, 150, 200):
        out[f'sma{n}'] = out.Close.rolling(n).mean()
    out['low252'] = out.Low.rolling(252).min()
    out['high252'] = out.High.rolling(252).max()
    out['prior_high20'] = out.High.shift(1).rolling(20).max()
    out['prior_volume20'] = out.Volume.shift(1).rolling(20).mean()
    # Exact-date alignment; no backward/forward fill of missing benchmark bars.
    base = benchmark.reindex(out.index)
    out['rs126'] = (out.Close / out.Close.shift(126)) / (base / base.shift(126)).where(lambda x: x > 0)
    out['ready'] = out[['Close', 'Volume', 'sma200', 'low252', 'high252', 'rs126', 'prior_high20', 'prior_volume20']].replace([np.inf, -np.inf], np.nan).notna().all(axis=1) & out.sma200.shift(20).notna()
    out['ma_pass'] = (out.Close > out.sma50) & (out.sma50 > out.sma150) & (out.sma150 > out.sma200)
    out['slope_pass'] = out.sma200 > out.sma200.shift(20)
    out['range_pass'] = (out.Close >= out.low252 * 1.3) & (out.Close >= out.high252 * .75)
    out['rs_pass'] = out.rs126 > 1
    out['template_pass'] = out.ready & out.ma_pass & out.slope_pass & out.range_pass & out.rs_pass
    out['breakout_pass'] = out.Close > out.prior_high20
    out['volume_pass'] = out.Volume > out.prior_volume20 * 1.5
    out['match'] = out.template_pass & out.breakout_pass & out.volume_pass
    out['reason'] = np.where(~out.ready, 'insufficient_data', np.where(out.match, 'match', 'conditions_not_met'))
    return out
