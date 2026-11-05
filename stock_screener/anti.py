"""Causal LBR Anti setup and hook state machines.

No entry, stop, exit, order, or P&L is emitted here. The same functions are
intended for the screener and a separately approved future backtest.
"""

from __future__ import annotations

import json
from typing import Literal

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_numeric_dtype

from .indicators import calculate_lbr_310, calculate_stochastic_7410
from .validation import MarketDataError

Direction = Literal["long", "short"]

STRATEGY_310 = "lbr_anti_310_adaptation_v1"
STRATEGY_STOCHASTIC = "lbr_anti_stochastic_v1"
DEFINITION_VERSION = "1.0"


def _validate_lines(fast: pd.Series, slow: pd.Series) -> tuple[pd.Series, pd.Series]:
    if not isinstance(fast, pd.Series) or not isinstance(slow, pd.Series):
        raise TypeError("fast and slow must be pandas Series")
    if not fast.index.equals(slow.index):
        raise MarketDataError("fast and slow must have identical indexes")
    if not fast.index.is_unique:
        raise MarketDataError("indicator index must be unique")
    if not fast.index.is_monotonic_increasing:
        raise MarketDataError("indicator index must be sorted ascending")
    for name, series in (("fast", fast), ("slow", slow)):
        if is_bool_dtype(series.dtype) or not is_numeric_dtype(series.dtype):
            raise MarketDataError(f"{name} must be numeric")
    clean_fast = fast.astype(float).replace([np.inf, -np.inf], np.nan)
    clean_slow = slow.astype(float).replace([np.inf, -np.inf], np.nan)
    return clean_fast, clean_slow


def detect_anti_from_lines(
    fast: pd.Series,
    slow: pd.Series,
    *,
    strategy_id: str,
    min_pullback_bars: int,
    allow_cross: bool,
    histogram: pd.Series | None = None,
    definition_version: str = DEFINITION_VERSION,
) -> pd.DataFrame:
    """Evaluate Anti setup and hook states from precomputed lines.

    The previous bar must have both lines moving in the trend direction, with
    fast on the trend side of slow. A candidate begins when fast turns against
    a still-trending slow line. A setup requires consecutive pullback bars and
    a smaller absolute fast/slow distance than immediately before the pullback.
    A hook is emitted once when fast reverses after a valid setup.
    """

    if isinstance(min_pullback_bars, bool) or not isinstance(min_pullback_bars, int):
        raise TypeError("min_pullback_bars must be an integer")
    if min_pullback_bars < 1:
        raise ValueError("min_pullback_bars must be positive")
    if not isinstance(allow_cross, bool):
        raise TypeError("allow_cross must be bool")

    fast, slow = _validate_lines(fast, slow)
    if histogram is None:
        histogram = fast - slow
    else:
        if not isinstance(histogram, pd.Series) or not histogram.index.equals(fast.index):
            raise MarketDataError("histogram must have the same index as fast")
        if is_bool_dtype(histogram.dtype) or not is_numeric_dtype(histogram.dtype):
            raise MarketDataError("histogram must be numeric")
        histogram = histogram.astype(float).replace([np.inf, -np.inf], np.nan)

    fast_slope = fast.diff()
    slow_slope = slow.diff()
    distance = (fast - slow).abs()
    parameter_set = json.dumps(
        {
            "allow_cross": allow_cross,
            "min_pullback_bars": min_pullback_bars,
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    rows: list[dict[str, object]] = []
    direction: Direction | None = None
    pullback_count = 0
    start_distance = np.nan
    setup_ready = False
    crossed_slow = False
    ever_indicator_ready = False

    def reset() -> None:
        nonlocal direction, pullback_count, start_distance, setup_ready, crossed_slow
        direction = None
        pullback_count = 0
        start_distance = np.nan
        setup_ready = False
        crossed_slow = False

    for position, timestamp in enumerate(fast.index):
        f = fast.iloc[position]
        s = slow.iloc[position]
        fs = fast_slope.iloc[position]
        ss = slow_slope.iloc[position]
        current_distance = distance.iloc[position]
        indicator_ready = bool(pd.notna(f) and pd.notna(s))
        warmup_ready = indicator_ready
        if indicator_ready:
            ever_indicator_ready = True

        phase = "idle"
        row_direction = ""
        reason = "no_candidate"
        row_count = 0
        row_crossed = False
        setup_long = False
        setup_short = False
        hook_long = False
        hook_short = False

        if not indicator_ready or pd.isna(fs) or pd.isna(ss):
            phase = "invalid" if ever_indicator_ready and not indicator_ready else "warmup"
            reason = "missing_data" if phase == "invalid" else "insufficient_history"
            reset()
        elif direction is not None:
            row_direction = direction
            row_count = pullback_count
            if direction == "long":
                crossed_now = bool(f < s)
                if crossed_now:
                    crossed_slow = True
                if crossed_now and not allow_cross:
                    phase, reason, row_crossed = "invalid", "cross_not_allowed", True
                    reset()
                elif ss <= 0.0:
                    phase, reason, row_crossed = "invalid", "slow_trend_broken", crossed_slow
                    reset()
                elif fs < 0.0:
                    pullback_count += 1
                    row_count = pullback_count
                    setup_ready = bool(
                        pullback_count >= min_pullback_bars
                        and current_distance < start_distance
                    )
                    setup_long = setup_ready
                    phase = "setup" if setup_ready else "pullback"
                    reason = "setup_ready" if setup_ready else "pullback_in_progress"
                    row_crossed = crossed_slow
                elif fs > 0.0 and setup_ready:
                    hook_long = True
                    phase, reason, row_crossed = "hook", "hook_confirmed", crossed_slow
                    reset()
                else:
                    phase = "invalid"
                    reason = "fast_flat" if fs == 0.0 else "pullback_too_short"
                    row_crossed = crossed_slow
                    reset()
            else:
                crossed_now = bool(f > s)
                if crossed_now:
                    crossed_slow = True
                if crossed_now and not allow_cross:
                    phase, reason, row_crossed = "invalid", "cross_not_allowed", True
                    reset()
                elif ss >= 0.0:
                    phase, reason, row_crossed = "invalid", "slow_trend_broken", crossed_slow
                    reset()
                elif fs > 0.0:
                    pullback_count += 1
                    row_count = pullback_count
                    setup_ready = bool(
                        pullback_count >= min_pullback_bars
                        and current_distance < start_distance
                    )
                    setup_short = setup_ready
                    phase = "setup" if setup_ready else "pullback"
                    reason = "setup_ready" if setup_ready else "pullback_in_progress"
                    row_crossed = crossed_slow
                elif fs < 0.0 and setup_ready:
                    hook_short = True
                    phase, reason, row_crossed = "hook", "hook_confirmed", crossed_slow
                    reset()
                else:
                    phase = "invalid"
                    reason = "fast_flat" if fs == 0.0 else "pullback_too_short"
                    row_crossed = crossed_slow
                    reset()
        else:
            pfs = fast_slope.iloc[position - 1] if position else np.nan
            pss = slow_slope.iloc[position - 1] if position else np.nan
            pf = fast.iloc[position - 1] if position else np.nan
            ps = slow.iloc[position - 1] if position else np.nan
            previous_distance = distance.iloc[position - 1] if position else np.nan

            starts_long = bool(
                position > 0
                and pd.notna(pfs)
                and pd.notna(pss)
                and pd.notna(pf)
                and pd.notna(ps)
                and pfs > 0.0
                and pss > 0.0
                and pf >= ps
                and fs < 0.0
                and ss > 0.0
            )
            starts_short = bool(
                position > 0
                and pd.notna(pfs)
                and pd.notna(pss)
                and pd.notna(pf)
                and pd.notna(ps)
                and pfs < 0.0
                and pss < 0.0
                and pf <= ps
                and fs > 0.0
                and ss < 0.0
            )

            if starts_long or starts_short:
                direction = "long" if starts_long else "short"
                pullback_count = 1
                start_distance = float(previous_distance)
                crossed_slow = bool(
                    (direction == "long" and f < s)
                    or (direction == "short" and f > s)
                )
                row_direction = direction
                row_count = 1
                row_crossed = crossed_slow
                if crossed_slow and not allow_cross:
                    phase, reason = "invalid", "cross_not_allowed"
                    reset()
                else:
                    setup_ready = bool(
                        min_pullback_bars == 1 and current_distance < start_distance
                    )
                    setup_long = bool(setup_ready and direction == "long")
                    setup_short = bool(setup_ready and direction == "short")
                    phase = "setup" if setup_ready else "pullback"
                    reason = "setup_ready" if setup_ready else "pullback_started"

        rows.append(
            {
                "strategy_id": strategy_id,
                "definition_version": definition_version,
                "parameter_set": parameter_set,
                "decision_timestamp": timestamp,
                "data_cutoff_timestamp": timestamp,
                "warmup_ready": warmup_ready,
                "phase": phase,
                "direction": row_direction,
                "reason": reason,
                "fast": f,
                "slow": s,
                "histogram": histogram.iloc[position],
                "fast_slope": fs,
                "slow_slope": ss,
                "distance_to_slow": current_distance,
                "pullback_count": row_count,
                "crossed_slow": row_crossed,
                "setup_long": setup_long,
                "setup_short": setup_short,
                "hook_long": hook_long,
                "hook_short": hook_short,
            }
        )

    return pd.DataFrame(rows, index=fast.index)


def evaluate_anti_310(
    frame: pd.DataFrame,
    *,
    pullback_bars: int = 3,
    allow_cross: bool = True,
) -> pd.DataFrame:
    """Evaluate the approved SMA 3/10 adaptation of Anti."""

    if isinstance(pullback_bars, bool) or pullback_bars not in (2, 3):
        raise ValueError("3/10 pullback_bars must be 2 or 3")
    indicators = calculate_lbr_310(frame)
    return detect_anti_from_lines(
        indicators["fast"],
        indicators["slow"],
        strategy_id=STRATEGY_310,
        min_pullback_bars=pullback_bars,
        allow_cross=allow_cross,
        histogram=indicators["histogram"],
    )


def evaluate_anti_stochastic(frame: pd.DataFrame) -> pd.DataFrame:
    """Evaluate the comparison stochastic 7/4/10 Anti setup."""

    indicators = calculate_stochastic_7410(frame)
    return detect_anti_from_lines(
        indicators["fast"],
        indicators["slow"],
        strategy_id=STRATEGY_STOCHASTIC,
        min_pullback_bars=3,
        allow_cross=True,
        histogram=indicators["histogram"],
    )
