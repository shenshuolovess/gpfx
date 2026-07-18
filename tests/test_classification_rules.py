import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from classification_rules import classify_label, rule_config_from_mapping


def make_row(**overrides) -> pd.Series:
    values = {
        "trend_score": 50.0,
        "direction_score": 0.0,
        "trend_stability_score": 40.0,
        "adx_score": 40.0,
        "position_score": 50.0,
        "rs_score": 0.0,
        "breakout_score": 0.0,
        "base_score": 0.0,
        "exhaustion_score": 0.0,
        "ma_structure_score": 0.0,
        "stabilize_score": 60.0,
        "R20": 0.0,
        "RS20": 0.0,
        "MA20": 10.0,
        "close": 11.0,
    }
    values.update(overrides)
    return pd.Series(values)


class ClassificationBoundaryTests(unittest.TestCase):
    def test_missing_metric_is_fuzzy(self):
        self.assertEqual(classify_label(make_row(RS20=np.nan)), "边界模糊")

    def test_top_threshold_and_strict_direction_boundary(self):
        top = make_row(
            position_score=88,
            exhaustion_score=82,
            trend_score=65,
            direction_score=20.0001,
        )
        self.assertEqual(classify_label(top), "赶顶")
        top["direction_score"] = 20
        self.assertEqual(classify_label(top), "震荡上行")

    def test_top_has_priority_over_rising(self):
        row = make_row(
            position_score=88,
            exhaustion_score=82,
            trend_score=72,
            direction_score=28,
            adx_score=55,
            rs_score=15,
            breakout_score=60,
        )
        self.assertEqual(classify_label(row), "赶顶")

    def test_base_threshold_and_price_confirmation(self):
        row = make_row(
            position_score=35,
            base_score=68,
            adx_score=50,
            direction_score=-44.999,
            stabilize_score=58,
            R20=-0.0199,
            RS20=-0.0599,
        )
        self.assertEqual(classify_label(row), "筑底")
        row["close"] = row["MA20"]
        self.assertEqual(classify_label(row), "过渡状态")

    def test_rising_threshold_and_exhaustion_upper_bound(self):
        row = make_row(
            trend_score=72,
            direction_score=28,
            adx_score=55,
            rs_score=15,
            breakout_score=60,
            exhaustion_score=87.999,
        )
        self.assertEqual(classify_label(row), "上升")
        row["exhaustion_score"] = 88
        self.assertEqual(classify_label(row), "边界模糊")

    def test_declining_threshold(self):
        row = make_row(
            trend_score=32,
            direction_score=-28,
            adx_score=50,
            rs_score=-15,
            breakout_score=-60,
        )
        self.assertEqual(classify_label(row), "下降")
        row["trend_score"] = 32.0001
        self.assertEqual(classify_label(row), "震荡下行")

    def test_oscillating_up_bounds(self):
        self.assertEqual(
            classify_label(make_row(trend_score=52, direction_score=10)),
            "震荡上行",
        )
        self.assertEqual(
            classify_label(make_row(trend_score=71.999, direction_score=10)),
            "震荡上行",
        )

    def test_oscillating_down_lower_bound_is_strict(self):
        self.assertEqual(
            classify_label(make_row(trend_score=30.0001, direction_score=-10)),
            "震荡下行",
        )
        self.assertEqual(
            classify_label(make_row(trend_score=30, direction_score=-10)),
            "边界模糊",
        )

    def test_sideways_includes_breakout_twenty(self):
        row = make_row(breakout_score=20)
        self.assertEqual(classify_label(row), "横盘")

    def test_transition_starts_at_direction_eighteen(self):
        row = make_row(direction_score=18)
        self.assertEqual(classify_label(row), "过渡状态")

    def test_candidate_override_does_not_change_baseline(self):
        row = make_row(
            trend_score=70,
            direction_score=25,
            adx_score=52,
            rs_score=12,
            breakout_score=60,
        )
        candidate = rule_config_from_mapping(
            {
                "rising_trend_min": 68,
                "rising_direction_min": 24,
                "rising_adx_min": 50,
                "rising_rs_min": 10,
            }
        )
        self.assertEqual(classify_label(row), "震荡上行")
        self.assertEqual(classify_label(row, candidate), "上升")

    def test_unknown_candidate_threshold_is_rejected(self):
        with self.assertRaises(KeyError):
            rule_config_from_mapping({"rising_typo": 1})


if __name__ == "__main__":
    unittest.main()
