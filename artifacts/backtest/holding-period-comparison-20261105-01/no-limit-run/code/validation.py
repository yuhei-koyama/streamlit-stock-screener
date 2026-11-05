"""Input validation shared by indicators and strategy evaluation."""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd
from pandas.api.types import is_bool_dtype, is_numeric_dtype


class MarketDataError(ValueError):
    """Raised when market data does not satisfy the strategy input contract."""


def validate_market_data(
    frame: pd.DataFrame, required_columns: Iterable[str]
) -> pd.DataFrame:
    """Validate and copy ascending, uniquely indexed numeric market data.

    Missing numeric values are allowed. Indicator calculation propagates them,
    and the Anti state machine resets at those positions instead of silently
    treating a missing observation as a valid setup.
    """

    if not isinstance(frame, pd.DataFrame):
        raise TypeError("market data must be a pandas DataFrame")

    required = tuple(required_columns)
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise MarketDataError(f"missing required columns: {', '.join(missing)}")

    if not frame.index.is_unique:
        raise MarketDataError("market data index must be unique")
    if not frame.index.is_monotonic_increasing:
        raise MarketDataError("market data index must be sorted ascending")

    for column in required:
        series = frame[column]
        if is_bool_dtype(series.dtype) or not is_numeric_dtype(series.dtype):
            raise MarketDataError(f"column {column!r} must be numeric")

    if "High" in required and "Low" in required:
        invalid_range = (
            frame["High"].notna()
            & frame["Low"].notna()
            & (frame["High"] < frame["Low"])
        )
        if bool(invalid_range.any()):
            raise MarketDataError("High must be greater than or equal to Low")

    return frame.copy(deep=True)
