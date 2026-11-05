"""Thin adapters calling the actual screener functions, never alternate formulas."""
from . import linda_versions, search

MINERVINI = 'minervini_screen_v1'
RSI2 = 'rsi2_screen_v1'
RSI14 = 'rsi14_screen_v1'
STRATEGIES = [*linda_versions.VERSIONS, MINERVINI, RSI2, RSI14]


def screening_signals(frame, strategy_id, benchmark=None):
    if strategy_id in linda_versions.VERSIONS:
        return linda_versions.evaluate_version(frame, strategy_id)
    if strategy_id == MINERVINI:
        if benchmark is None:
            raise ValueError('1306.T benchmark required')
        out = search.search_minervini(frame, benchmark)
    elif strategy_id in (RSI2, RSI14):
        out = search.search_rsi(frame, period=2 if strategy_id == RSI2 else 14,
                                trend_filter=True)
    else:
        raise ValueError(f'Unknown strategy: {strategy_id}')
    out['buy_signal'] = out['match']
    out['sell_signal'] = False  # These search pages do not implement SELL.
    out['strategy_id'] = strategy_id
    return out
