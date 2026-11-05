import unittest

import numpy as np
import pandas as pd

from stock_screener.indicators import (
    calculate_lbr_310,
    calculate_stochastic_7410,
)
from stock_screener.validation import MarketDataError, validate_market_data


class Lbr310IndicatorTests(unittest.TestCase):
    def test_manual_values_and_full_window_boundaries(self):
        frame = pd.DataFrame({"Close": np.arange(1.0, 31.0)})

        result = calculate_lbr_310(frame)

        self.assertTrue(result["fast"].iloc[:9].isna().all())
        self.assertAlmostEqual(result["sma_3"].iloc[9], 9.0)
        self.assertAlmostEqual(result["sma_10"].iloc[9], 5.5)
        self.assertAlmostEqual(result["fast"].iloc[9], 3.5)
        self.assertTrue(result["slow"].iloc[:24].isna().all())
        self.assertAlmostEqual(result["slow"].iloc[24], 3.5)
        self.assertAlmostEqual(result["histogram"].iloc[24], 0.0)

    def test_uses_sma_not_ema_and_does_not_mutate_input(self):
        close = pd.Series([1.0, 3.0, 2.0, 8.0, 5.0, 13.0, 3.0, 21.0, 1.0, 34.0, 2.0])
        frame = pd.DataFrame({"Close": close})
        original = frame.copy(deep=True)

        result = calculate_lbr_310(frame)
        ema_difference = close.ewm(span=3, adjust=False).mean() - close.ewm(
            span=10, adjust=False
        ).mean()

        self.assertNotAlmostEqual(result["fast"].iloc[10], ema_difference.iloc[10])
        pd.testing.assert_frame_equal(frame, original)

    def test_future_rows_do_not_change_past_values(self):
        frame = pd.DataFrame({"Close": np.sin(np.arange(50) / 3.0) + np.arange(50)})
        prefix = calculate_lbr_310(frame.iloc[:35])
        full = calculate_lbr_310(frame)
        pd.testing.assert_frame_equal(prefix, full.iloc[:35])


class StochasticIndicatorTests(unittest.TestCase):
    def test_manual_values_and_full_window_boundaries(self):
        base = np.arange(1.0, 31.0)
        frame = pd.DataFrame({"High": base + 2.0, "Low": base, "Close": base + 1.0})

        result = calculate_stochastic_7410(frame)

        self.assertTrue(result["raw_k"].iloc[:6].isna().all())
        self.assertAlmostEqual(result["raw_k"].iloc[6], 87.5)
        self.assertTrue(result["fast"].iloc[:9].isna().all())
        self.assertAlmostEqual(result["fast"].iloc[9], 87.5)
        self.assertTrue(result["slow"].iloc[:18].isna().all())
        self.assertAlmostEqual(result["slow"].iloc[18], 87.5)

    def test_zero_range_remains_missing(self):
        frame = pd.DataFrame(
            {"High": np.ones(30), "Low": np.ones(30), "Close": np.ones(30)}
        )
        result = calculate_stochastic_7410(frame)
        self.assertTrue(result[["raw_k", "fast", "slow"]].isna().all().all())


class ValidationTests(unittest.TestCase):
    def test_rejects_missing_nonnumeric_duplicate_and_descending_data(self):
        with self.assertRaises(MarketDataError):
            validate_market_data(pd.DataFrame({"Open": [1.0]}), ("Close",))
        with self.assertRaises(MarketDataError):
            validate_market_data(pd.DataFrame({"Close": ["1"]}), ("Close",))
        with self.assertRaises(MarketDataError):
            validate_market_data(
                pd.DataFrame({"Close": [1.0, 2.0]}, index=[0, 0]), ("Close",)
            )
        with self.assertRaises(MarketDataError):
            validate_market_data(
                pd.DataFrame({"Close": [1.0, 2.0]}, index=[1, 0]), ("Close",)
            )

    def test_rejects_invalid_high_low_range(self):
        frame = pd.DataFrame({"High": [1.0], "Low": [2.0], "Close": [1.5]})
        with self.assertRaises(MarketDataError):
            validate_market_data(frame, ("High", "Low", "Close"))


if __name__ == "__main__":
    unittest.main()
