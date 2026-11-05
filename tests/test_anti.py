import unittest

import numpy as np
import pandas as pd

from stock_screener.anti import (
    STRATEGY_310,
    detect_anti_from_lines,
    evaluate_anti_310,
    evaluate_anti_stochastic,
)


def detect(fast_values, slow_values, *, bars=3, allow_cross=True):
    index = pd.date_range("2024-01-01", periods=len(fast_values), freq="D")
    return detect_anti_from_lines(
        pd.Series(fast_values, index=index, dtype=float),
        pd.Series(slow_values, index=index, dtype=float),
        strategy_id=STRATEGY_310,
        min_pullback_bars=bars,
        allow_cross=allow_cross,
    )


class AntiStateMachineTests(unittest.TestCase):
    def test_long_setup_then_single_hook(self):
        fast = [1, 2, 4, 6, 5.5, 5.0, 4.5, 5.0, 5.5]
        slow = [0, 1, 2, 3, 4, 5, 6, 7, 8]

        result = detect(fast, slow)

        self.assertEqual(result["phase"].iloc[6], "setup")
        self.assertTrue(result["setup_long"].iloc[6])
        self.assertEqual(result["pullback_count"].iloc[6], 3)
        self.assertEqual(result["phase"].iloc[7], "hook")
        self.assertTrue(result["hook_long"].iloc[7])
        self.assertEqual(int(result["hook_long"].sum()), 1)
        self.assertFalse(result["hook_short"].any())

    def test_two_bar_mode_boundary(self):
        fast = [1, 2, 4, 6, 5.5, 5.0, 5.5]
        slow = [0, 1, 2, 3, 4, 5, 6]

        two = detect(fast, slow, bars=2)
        three = detect(fast, slow, bars=3)

        self.assertTrue(two["hook_long"].iloc[-1])
        self.assertFalse(three["hook_long"].iloc[-1])
        self.assertEqual(three["reason"].iloc[-1], "pullback_too_short")

    def test_short_is_exact_sign_mirror(self):
        fast = np.array([1, 2, 4, 6, 5.5, 5.0, 4.5, 5.0], dtype=float)
        slow = np.arange(8, dtype=float)
        long_result = detect(fast, slow)
        short_result = detect(-fast, -slow)

        self.assertListEqual(
            long_result["setup_long"].tolist(), short_result["setup_short"].tolist()
        )
        self.assertListEqual(
            long_result["hook_long"].tolist(), short_result["hook_short"].tolist()
        )
        self.assertListEqual(
            long_result["pullback_count"].tolist(),
            short_result["pullback_count"].tolist(),
        )

    def test_touch_is_allowed_but_cross_can_be_forbidden(self):
        touch_fast = [1, 2, 4, 6, 5.5, 5.0, 4.5, 5.0]
        slow = [0, 1, 2, 3, 4, 5, 6, 7]
        touch = detect(touch_fast, slow, allow_cross=False)
        self.assertNotEqual(touch["reason"].iloc[5], "cross_not_allowed")

        cross_fast = [1, 2, 4, 6, 5.0, 4.5, 4.0, 4.5]
        allowed = detect(cross_fast, slow, allow_cross=True)
        forbidden = detect(cross_fast, slow, allow_cross=False)
        self.assertTrue(allowed["hook_long"].iloc[-1])
        self.assertTrue(allowed["crossed_slow"].iloc[-1])
        self.assertFalse(forbidden["hook_long"].any())
        self.assertIn("cross_not_allowed", forbidden["reason"].tolist())

    def test_flat_fast_slow_reversal_and_missing_value_reset_state(self):
        common_slow = [0, 1, 2, 3, 4, 5, 6]
        flat = detect([1, 2, 4, 6, 5.5, 5.5, 6], common_slow, bars=2)
        self.assertEqual(flat["reason"].iloc[5], "fast_flat")
        self.assertFalse(flat["hook_long"].any())

        slow_reversal = detect(
            [1, 2, 4, 6, 5.5, 5.0, 5.5], [0, 1, 2, 3, 4, 3.5, 4], bars=2
        )
        self.assertEqual(slow_reversal["reason"].iloc[5], "slow_trend_broken")
        self.assertFalse(slow_reversal["hook_long"].any())

        missing = detect([1, 2, 4, 6, 5.5, np.nan, 5.0], common_slow, bars=2)
        self.assertEqual(missing["reason"].iloc[5], "missing_data")
        self.assertFalse(missing["hook_long"].any())

    def test_distance_must_shrink(self):
        fast = [1, 2, 4, 6, 5, 2, 2.5]
        slow = [0, 1, 2, 3, 4, 5, 6]
        result = detect(fast, slow, bars=2)
        self.assertFalse(result["setup_long"].any())
        self.assertFalse(result["hook_long"].any())

    def test_future_append_does_not_change_past_state(self):
        fast = [1, 2, 4, 6, 5.5, 5.0, 4.5, 5.0, 100.0, -100.0]
        slow = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
        prefix = detect(fast[:8], slow[:8])
        full = detect(fast, slow)
        pd.testing.assert_frame_equal(prefix, full.iloc[:8])


class PublicContractTests(unittest.TestCase):
    def test_strategy_output_has_audit_fields_and_no_trading_fields(self):
        close = np.sin(np.arange(60) / 3.0) + np.arange(60) / 10.0 + 100.0
        frame = pd.DataFrame({"Close": close})
        result = evaluate_anti_310(frame)

        required = {
            "strategy_id",
            "definition_version",
            "parameter_set",
            "decision_timestamp",
            "data_cutoff_timestamp",
            "warmup_ready",
            "phase",
            "direction",
            "reason",
            "fast",
            "slow",
            "histogram",
            "fast_slope",
            "slow_slope",
            "distance_to_slow",
            "pullback_count",
            "crossed_slow",
            "setup_long",
            "setup_short",
            "hook_long",
            "hook_short",
        }
        self.assertTrue(required.issubset(result.columns))
        self.assertTrue((result["strategy_id"] == STRATEGY_310).all())
        forbidden = {"entry", "stop", "exit", "order", "pnl"}
        self.assertTrue(forbidden.isdisjoint(result.columns))

    def test_public_parameter_contract(self):
        frame = pd.DataFrame({"Close": np.arange(40.0)})
        with self.assertRaises(ValueError):
            evaluate_anti_310(frame, pullback_bars=1)

    def test_stochastic_uses_fixed_three_bar_parameter(self):
        base = np.arange(40.0)
        frame = pd.DataFrame({"High": base + 2, "Low": base, "Close": base + 1})
        result = evaluate_anti_stochastic(frame)
        self.assertTrue(
            result["parameter_set"].str.contains('"min_pullback_bars":3').all()
        )

    def test_public_310_is_causal_and_sign_symmetric(self):
        rng = np.random.default_rng(0)
        close = 100.0 + np.cumsum(rng.normal(0.05, 1.0, 160))
        index = pd.date_range("2024-01-01", periods=len(close), freq="D")
        frame = pd.DataFrame({"Close": close}, index=index)

        full = evaluate_anti_310(frame)
        prefix = evaluate_anti_310(frame.iloc[:120])
        pd.testing.assert_frame_equal(prefix, full.iloc[:120])

        mirrored = evaluate_anti_310(pd.DataFrame({"Close": 250.0 - close}, index=index))
        self.assertGreater(int(full[["hook_long", "hook_short"]].to_numpy().sum()), 0)
        self.assertListEqual(full["setup_long"].tolist(), mirrored["setup_short"].tolist())
        self.assertListEqual(full["setup_short"].tolist(), mirrored["setup_long"].tolist())
        self.assertListEqual(full["hook_long"].tolist(), mirrored["hook_short"].tolist())
        self.assertListEqual(full["hook_short"].tolist(), mirrored["hook_long"].tolist())
        self.assertListEqual(
            full["pullback_count"].tolist(), mirrored["pullback_count"].tolist()
        )

    def test_public_stochastic_is_causal_and_price_symmetric(self):
        rng = np.random.default_rng(0)
        close = 100.0 + np.cumsum(rng.normal(0.05, 1.0, 160))
        spread = rng.uniform(0.5, 2.0, len(close))
        index = pd.date_range("2024-01-01", periods=len(close), freq="D")
        frame = pd.DataFrame(
            {"High": close + spread, "Low": close - spread, "Close": close},
            index=index,
        )

        full = evaluate_anti_stochastic(frame)
        prefix = evaluate_anti_stochastic(frame.iloc[:120])
        pd.testing.assert_frame_equal(prefix, full.iloc[:120])

        level = 250.0
        mirrored_frame = pd.DataFrame(
            {
                "High": level - frame["Low"],
                "Low": level - frame["High"],
                "Close": level - frame["Close"],
            },
            index=index,
        )
        mirrored = evaluate_anti_stochastic(mirrored_frame)
        self.assertGreater(int(full[["hook_long", "hook_short"]].to_numpy().sum()), 0)
        self.assertListEqual(full["setup_long"].tolist(), mirrored["setup_short"].tolist())
        self.assertListEqual(full["setup_short"].tolist(), mirrored["setup_long"].tolist())
        self.assertListEqual(full["hook_long"].tolist(), mirrored["hook_short"].tolist())
        self.assertListEqual(full["hook_short"].tolist(), mirrored["hook_long"].tolist())
        np.testing.assert_allclose(
            full["fast"].dropna().to_numpy()
            + mirrored.loc[full["fast"].notna(), "fast"].to_numpy(),
            100.0,
        )


if __name__ == "__main__":
    unittest.main()
