"""Causal indicator calculations used by UI and future backtests."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .validation import validate_market_data


def _finite(series: pd.Series) -> pd.Series:
    return series.astype(float).replace([np.inf, -np.inf], np.nan)


def calculate_lbr_310(frame: pd.DataFrame) -> pd.DataFrame:
    """Calculate SMA(Close,3)-SMA(Close,10) and its SMA(16)."""

    result = validate_market_data(frame, ("Close",))
    close = _finite(result["Close"])
    result["sma_3"] = close.rolling(window=3, min_periods=3).mean()
    result["sma_10"] = close.rolling(window=10, min_periods=10).mean()
    result["fast"] = result["sma_3"] - result["sma_10"]
    result["slow"] = result["fast"].rolling(window=16, min_periods=16).mean()
    result["histogram"] = result["fast"] - result["slow"]
    return result


def calculate_stochastic_7410(frame: pd.DataFrame) -> pd.DataFrame:
    """Calculate Raw %K(7), its SMA(4), then the fast line's SMA(10)."""

    result = validate_market_data(frame, ("High", "Low", "Close"))
    high = _finite(result["High"])
    low = _finite(result["Low"])
    close = _finite(result["Close"])
    highest = high.rolling(window=7, min_periods=7).max()
    lowest = low.rolling(window=7, min_periods=7).min()
    denominator = (highest - lowest).where((highest - lowest) != 0.0)

    result["raw_k"] = 100.0 * (close - lowest) / denominator
    result["fast"] = result["raw_k"].rolling(window=4, min_periods=4).mean()
    result["slow"] = result["fast"].rolling(window=10, min_periods=10).mean()
    result["histogram"] = result["fast"] - result["slow"]
    return result
