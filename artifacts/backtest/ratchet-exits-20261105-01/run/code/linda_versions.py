"""Versioned, causal screening signals; no fills, positions, exits or P&L."""
from __future__ import annotations

import json
import numpy as np
import pandas as pd

from .anti import evaluate_anti_310, evaluate_anti_stochastic
from .validation import validate_market_data

TV = 'tv_ema_stoch_v1'
ANTI_310 = 'lbr_anti_310_adaptation_v1'
ANTI_STOCH = 'lbr_anti_stochastic_v1'
LEGACY = 'legacy_ema_sma16_v1'
VERSIONS = {
    TV: ('TradingView添付版', 'user_pine_v6_2026-11-05'),
    ANTI_310: ('Anti 3/10適応版', 'lbr-anti-logic_1.0-approved'),
    ANTI_STOCH: ('Anti 7/4/10版', 'lbr-anti-logic_1.0-approved'),
    LEGACY: ('初期試作版', 'git:8f7aa99'),
}
TV_DEFAULTS = dict(fast_len=3, slow_len=10, signal_len=16,
                   k_len=7, d_len=10, smooth_k=3)


def _cross(fast, slow):
    return ((fast > slow) & (fast.shift(1) <= slow.shift(1)),
            (fast < slow) & (fast.shift(1) >= slow.shift(1)))


def _tv_signals(out):
    out['macd_buy'], out['macd_sell'] = _cross(out.fast, out.slow)
    out['stoch_buy_hook'] = (out.k > out.k.shift(1)) & (out.k.shift(1) < out.k.shift(2)) & (out.d > out.d.shift(1))
    out['stoch_sell_hook'] = (out.k < out.k.shift(1)) & (out.k.shift(1) > out.k.shift(2)) & (out.d < out.d.shift(1))
    out['buy_signal'] = out.macd_buy & out.stoch_buy_hook
    out['sell_signal'] = out.macd_sell & out.stoch_sell_hook
    return out


def evaluate_tv(frame, **parameters):
    unknown = parameters.keys() - TV_DEFAULTS.keys()
    if unknown:
        raise ValueError(f'Unknown TV parameters: {sorted(unknown)}')
    settings = TV_DEFAULTS | parameters
    for name, value in settings.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f'{name} must be a positive integer')
    out = validate_market_data(frame, ('High', 'Low', 'Close'))
    clean = out[['High', 'Low', 'Close']].replace([np.inf, -np.inf], np.nan)
    valid = clean.notna().all(axis=1)
    # Restart recursive state after invalid OHLC; do not bridge missing observations.
    group = (~valid).cumsum()
    for column in ('fast', 'slow', 'histogram', 'raw_k', 'k', 'd'):
        out[column] = np.nan
    for _, part in clean.loc[valid].groupby(group[valid]):
        close = part.Close
        fast = close.ewm(span=settings['fast_len'], adjust=False).mean() - close.ewm(span=settings['slow_len'], adjust=False).mean()
        slow = fast.ewm(span=settings['signal_len'], adjust=False).mean()
        # Pine highest/lowest use available bars at the start of a series.
        highest = part.High.rolling(settings['k_len'], min_periods=1).max()
        lowest = part.Low.rolling(settings['k_len'], min_periods=1).min()
        raw = 100 * (close - lowest) / (highest - lowest).replace(0, np.nan)
        k = raw.rolling(settings['smooth_k']).mean()
        d = k.rolling(settings['d_len']).mean()
        for name, values in dict(fast=fast, slow=slow, histogram=fast-slow, raw_k=raw, k=k, d=d).items():
            out.loc[part.index, name] = values
    out = _tv_signals(out)
    out['ready'] = (out[['fast', 'slow', 'k', 'd']].notna().all(axis=1)
                    & out.k.shift(2).notna() & out.d.shift(1).notna()
                    & out.fast.shift(1).notna() & out.slow.shift(1).notna())
    out['buy_signal'] &= out.ready
    out['sell_signal'] &= out.ready
    return out, settings


def evaluate_legacy(frame):
    """Effective formulas from initial app.py, including its 30-row minimum."""
    out = validate_market_data(frame, ('Close',))
    close = out.Close
    out['fast'] = close.ewm(span=3, adjust=False).mean() - close.ewm(span=10, adjust=False).mean()
    out['slow'] = out.fast.rolling(16).mean()
    out['histogram'] = out.fast - out.slow
    out['sma16'] = close.rolling(16).mean()
    out['ready'] = (pd.Series(np.arange(len(out)) >= 29, index=out.index)
                    & out[['fast', 'slow', 'histogram', 'sma16']].notna().all(axis=1)
                    & out.histogram.shift(2).notna() & out.histogram.shift(1).notna())
    out['gc'], out['dc'] = _cross(out.fast, out.slow)
    out['gc'] &= out.ready
    out['dc'] &= out.ready
    hist = out.histogram
    out['buy_signal'] = out.ready & (hist.shift(2) > hist.shift(1)) & (hist > hist.shift(1)) & (close > out.sma16)
    out['sell_signal'] = out.ready & (hist.shift(2) < hist.shift(1)) & (hist < hist.shift(1)) & (close < out.sma16)
    return out, dict(fast_len=3, slow_len=10, signal_sma=16, price_sma=16, min_rows=30)


def evaluate_version(frame, strategy_id, **parameters):
    """Return per-bar signals with explicit definition, source and parameter identity.

    buy/sell_signal represent that version's candidates, not executable orders.
    For Anti they map to Hook Long/Short; setup flags retain their separate meaning.
    """
    if strategy_id not in VERSIONS:
        raise ValueError(f'Unknown strategy: {strategy_id}')
    if strategy_id == TV:
        out, settings = evaluate_tv(frame, **parameters)
    elif strategy_id == LEGACY:
        if parameters:
            raise ValueError('Legacy parameters are fixed to the original implementation')
        out, settings = evaluate_legacy(frame)
    else:
        if strategy_id == ANTI_310:
            out = evaluate_anti_310(frame, **parameters)
        else:
            if parameters:
                raise ValueError('Stochastic Anti parameters are fixed')
            out = evaluate_anti_stochastic(frame)
        settings = json.loads(out.parameter_set.iloc[0]) if len(out) else parameters
        out['buy_signal'] = out.hook_long
        out['sell_signal'] = out.hook_short
        out['ready'] = out.warmup_ready & out.fast_slope.notna() & out.slow_slope.notna()
    out['strategy_id'] = strategy_id
    out['definition_version'] = '1.0'
    out['source_version'] = VERSIONS[strategy_id][1]
    out['parameter_set'] = json.dumps(settings, sort_keys=True, separators=(',', ':'))
    out['decision_timestamp'] = out.index
    return out
