import sys
import unittest
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backtest_classification import (
    add_pool_relative_returns,
    benchmark_forward_returns,
    choose_snapshot_dates,
    summarize,
    trimmed_mean,
)


class BacktestWindowTests(unittest.TestCase):
    def test_step_must_be_positive(self):
        benchmark = pd.DataFrame({"date": [f"d{i:03d}" for i in range(20)]})
        with self.assertRaises(ValueError):
            choose_snapshot_dates(
                benchmark, minimum_history=3, max_horizon=2,
                snapshots=2, step=0,
            )

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


class RobustSummaryTests(unittest.TestCase):
    def setUp(self):
        self.detail = pd.DataFrame(
            {
                "代码": ["A", "B", "A", "C"],
                "回测截面日": ["d1", "d1", "d2", "d2"],
                "分类": ["上升", "横盘", "上升", "横盘"],
                "未来5日收益": [0.10, 0.00, 0.20, -0.10],
                "未来5日超额": [0.08, -0.02, 0.18, -0.12],
            }
        )

    def test_pool_relative_return_is_calculated_per_snapshot(self):
        result = add_pool_relative_returns(self.detail, [5])
        self.assertEqual(result["同池5日平均收益"].round(4).tolist(), [0.05] * 4)
        self.assertEqual(result["未来5日同池超额"].round(4).tolist(), [0.05, -0.05, 0.15, -0.15])

    def test_summary_contains_robustness_and_quality_fields(self):
        detail = add_pool_relative_returns(self.detail, [5])
        result = summarize(detail, [5], step=3)
        rising = result[result["分类"] == "上升"].iloc[0]
        self.assertAlmostEqual(rising["平均同池超额"], 0.10)
        self.assertEqual(rising["不同股票数"], 1)
        self.assertEqual(rising["覆盖截面数"], 2)
        self.assertEqual(rising["截面间隔"], 3)
        self.assertEqual(rising["窗口是否重叠"], "是")
        self.assertEqual(rising["可信度"], "低")
        self.assertIn("截面少于10个", rising["数据质量提示"])

    def test_trimmed_mean_removes_both_tails(self):
        values = pd.Series([-10, *range(1, 9), 100])
        self.assertAlmostEqual(trimmed_mean(values), 4.5)


if __name__ == "__main__":
    unittest.main()
