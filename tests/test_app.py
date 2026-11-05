import unittest
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

import app


class AppContractTests(unittest.TestCase):
    def test_default_page_starts_without_network_access(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        rendered = AppTest.from_file(str(app_path), default_timeout=10).run()

        self.assertEqual(rendered.title[0].value, "LBR Anti ロジック検証")
        self.assertEqual(len(rendered.exception), 0)

    def test_ticker_normalization_is_tse_only_and_deduplicated(self):
        tickers, invalid = app.normalize_tickers("7203, 7203.T、1306  AAPL")

        self.assertEqual(tickers, ["7203.T", "1306.T"])
        self.assertEqual(invalid, ["AAPL"])

    def test_current_day_bar_is_removed_only_when_enabled(self):
        today = pd.Timestamp.now(tz="Asia/Tokyo").normalize()
        index = pd.DatetimeIndex([today - pd.DateOffset(days=1), today])
        frame = pd.DataFrame({"Close": [100.0, 101.0]}, index=index)

        removed = app.exclude_current_day_bar(frame, True)
        preserved = app.exclude_current_day_bar(frame, False)

        self.assertEqual(len(removed), 1)
        pd.testing.assert_frame_equal(preserved, frame)


if __name__ == "__main__":
    unittest.main()
