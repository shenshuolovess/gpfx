import sys
import unittest
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backtest_classification import benchmark_forward_returns, choose_snapshot_dates


class BacktestWindowTests(unittest.TestCase):
    def test_snapshot_selection_reserves_future_window(self):
        benchmark = pd.DataFrame({"date": [f"d{i:03d}" for i in range(300)]})
        dates = choose_snapshot_dates(
            benchmark,
            minimum_history=220,
            max_horizon=60,
            snapshots=2,
            step=20,
        )
        self.assertEqual(dates, ["d219", "d239"])

    def test_zero_snapshots_uses_all_available_positions(self):
        benchmark = pd.DataFrame({"date": [f"d{i:03d}" for i in range(10)]})
        dates = choose_snapshot_dates(
            benchmark,
            minimum_history=3,
            max_horizon=2,
            snapshots=0,
            step=2,
        )
        self.assertEqual(dates, ["d003", "d005", "d007"])

    def test_forward_return_uses_rows_after_snapshot(self):
        benchmark = pd.DataFrame(
            {
                "date": ["d0", "d1", "d2", "d3"],
                "close": [100, 110, 121, 133.1],
            }
        )
        returns = benchmark_forward_returns(benchmark, ["d0"], [1, 2])
        self.assertAlmostEqual(returns[("d0", 1)], 0.10)
        self.assertAlmostEqual(returns[("d0", 2)], 0.21)


if __name__ == "__main__":
    unittest.main()
