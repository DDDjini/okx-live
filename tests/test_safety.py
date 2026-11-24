import os
import unittest
from unittest.mock import patch

import pandas as pd

import live_trader


class FractalWindowTests(unittest.TestCase):
    def test_5_left_2_right_bottom_fractal(self):
        lows = [12, 11, 10, 9, 8, 7, 1, 6, 8, 9, 10, 11]
        df = pd.DataFrame({"low": lows, "high": [x + 2 for x in lows]})
        out = live_trader.add_fractals(df, 5, 2)
        self.assertTrue(bool(out.loc[6, "fractal_low"]))
        self.assertFalse(bool(out.loc[6, "fractal_high"]))

    def test_incomplete_right_window_is_not_confirmed(self):
        lows = [12, 11, 10, 9, 8, 7, 1, 6]
        df = pd.DataFrame({"low": lows, "high": [x + 2 for x in lows]})
        out = live_trader.add_fractals(df, 5, 2)
        self.assertFalse(bool(out.loc[6, "fractal_low"]))

    def test_incomplete_left_window_is_not_confirmed(self):
        lows = [1, 12, 11, 10, 9, 8, 7, 9, 10, 11]
        df = pd.DataFrame({"low": lows, "high": [x + 2 for x in lows]})
        out = live_trader.add_fractals(df, 5, 2)
        self.assertFalse(bool(out.loc[0, "fractal_low"]))

    def test_5_left_2_right_top_fractal(self):
        highs = [1, 2, 3, 4, 5, 6, 20, 7, 5, 4, 3, 2]
        df = pd.DataFrame({"high": highs, "low": [x - 2 for x in highs]})
        out = live_trader.add_fractals(df, 5, 2)
        self.assertTrue(bool(out.loc[6, "fractal_high"]))
        self.assertFalse(bool(out.loc[6, "fractal_low"]))


class ExecutionGateTests(unittest.TestCase):
    def test_live_execution_is_disabled_without_explicit_flag(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(live_trader, "OKXTrader") as trader:
            live_trader.run_once()
            trader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
