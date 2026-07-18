import sys
import unittest
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compare_classification_rules import (
    assign_time_segments,
    build_baseline_deltas,
    load_rule_configs,
)


class RuleComparisonTests(unittest.TestCase):
    def test_time_split_is_chronological_60_20_20(self):
        dates = [f"2026-01-{day:02d}" for day in range(1, 11)]
        segments = assign_time_segments(dates)
        self.assertEqual(list(segments.values()).count("训练期"), 6)
        self.assertEqual(list(segments.values()).count("验证期"), 2)
        self.assertEqual(list(segments.values()).count("测试期"), 2)

    def test_project_candidate_config_loads_with_baseline(self):
        rules, descriptions = load_rule_configs(
            PROJECT_ROOT / "classification_rule_configs.toml",
            ["relaxed_rising"],
        )
        self.assertEqual(set(rules), {"baseline", "relaxed_rising"})
        self.assertIn("放宽", descriptions["relaxed_rising"])
        self.assertEqual(rules["baseline"].rising_trend_min, 72)
        self.assertEqual(rules["relaxed_rising"].rising_trend_min, 68)

    def test_baseline_delta_is_candidate_minus_baseline(self):
        rows = []
        for rule, mean_return in (("baseline", 0.10), ("candidate", 0.13)):
            rows.append(
                {
                    "样本区间": "总体",
                    "规则": rule,
                    "分类": "上升",
                    "周期": "20日",
                    "样本数": 10,
                    "平均收益": mean_return,
                    "中位收益": mean_return,
                    "上涨胜率": 0.6,
                    "平均超额": mean_return,
                    "中位超额": mean_return,
                    "跑赢基准率": 0.6,
                    "平均信号期最大回撤": -0.05,
                }
            )
        result = build_baseline_deltas(pd.DataFrame(rows))
        self.assertAlmostEqual(result.iloc[0]["候选减基线_平均收益"], 0.03)


if __name__ == "__main__":
    unittest.main()
