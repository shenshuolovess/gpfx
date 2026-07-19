import sys
import unittest
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compare_classification_rules import (
    add_change_triggers,
    assign_time_segments,
    build_baseline_deltas,
    build_threshold_changes,
    summarize_performance,
    summarize_changed_samples,
    summarize_changed_stocks,
    load_rule_configs,
)


class RuleComparisonTests(unittest.TestCase):
    def candidate_rules(self):
        return load_rule_configs(
            PROJECT_ROOT / "classification_rule_configs.toml", ["relaxed_rising"]
        )[0]

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

    def test_threshold_changes_and_sample_triggers_are_explicit(self):
        rules = self.candidate_rules()
        changes = build_threshold_changes(rules)
        self.assertEqual(set(changes["参数"]), {
            "rising_trend_min", "rising_direction_min", "rising_adx_min", "rising_rs_min",
        })
        row = {
            "relaxed_rising是否变化": True,
            "trend_score": 70, "direction_score": 30,
            "adx_score": 60, "rs_score": 20,
        }
        detail = add_change_triggers(pd.DataFrame([row]), rules)
        self.assertIn("趋势分70.0>=68（基线72，新通过）", detail.iloc[0]["relaxed_rising触发阈值"])

    def test_migration_summary_has_periods_quality_and_ranked_stocks(self):
        rules = self.candidate_rules()
        rows = []
        for code, value in (("000001.SZ", 0.04), ("000002.SZ", -0.02)):
            rows.append({
                "代码": code, "名称": code, "回测截面日": "2026-01-01",
                "样本区间": "测试期", "baseline分类": "边界模糊",
                "relaxed_rising分类": "上升", "relaxed_rising是否变化": True,
                "relaxed_rising触发阈值": "趋势分70.0>=68（基线72，新通过）",
                "未来5日收益": value, "未来5日超额": value - 0.01,
                "未来5日同池超额": value,
            })
        detail = pd.DataFrame(rows)
        summary = summarize_changed_samples(
            detail, rules, [5], bootstrap_iterations=10, step=5,
        )
        self.assertEqual(set(summary["样本区间"]), {"总体", "测试期"})
        self.assertIn("平均同池超额", summary.columns)
        self.assertIn("数据质量提示", summary.columns)
        stocks = summarize_changed_stocks(detail, rules, [5], limit=1)
        self.assertEqual(set(stocks["榜单"]), {"正向榜", "负向榜"})
        self.assertEqual(stocks[stocks["榜单"] == "正向榜"].iloc[0]["代码"], "000001.SZ")

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
                    "平均同池超额": mean_return,
                    "跑赢同池率": 0.6,
                    "平均信号期最大回撤": -0.05,
                }
            )
        result = build_baseline_deltas(pd.DataFrame(rows))
        self.assertAlmostEqual(result.iloc[0]["候选减基线_平均收益"], 0.03)

    def test_performance_summary_has_same_pool_and_quality_metrics(self):
        rows = []
        for date, values in (("2026-01-01", (0.10, -0.10)), ("2026-02-01", (0.20, 0.00))):
            for index, value in enumerate(values):
                rows.append({
                    "代码": f"00000{index}.SZ", "回测截面日": date,
                    "样本区间": "测试期", "规则": "baseline", "分类": "上升",
                    "相对基线发生变化": False,
                    "未来20日收益": value, "未来20日超额": value - 0.01,
                    "未来20日同池超额": value - sum(values) / len(values),
                    "未来20日最大回撤": -0.05,
                })
        summary = summarize_performance(
            pd.DataFrame(rows), [20], min_samples=2,
            bootstrap_iterations=20, seed=7, step=20,
        )
        overall = summary[summary["样本区间"] == "总体"].iloc[0]
        self.assertIn("平均同池超额", summary.columns)
        self.assertIn("同池超额95%CI下限", summary.columns)
        self.assertIn("统计结论", summary.columns)
        self.assertEqual(overall["覆盖截面数"], 2)
        self.assertEqual(overall["窗口是否重叠"], "否")
        self.assertEqual(overall["可信度"], "低")


if __name__ == "__main__":
    unittest.main()
