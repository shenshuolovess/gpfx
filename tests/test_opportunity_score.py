import sys
import unittest
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backtest_opportunity_score import add_scores_and_buckets, summarize_ranking_quality
from opportunity_score import (
    OpportunityConfig, add_opportunity_scores, opportunity_output, score_opportunity,
)
from validate_opportunity_factors import (
    add_minimal_model_scores, rolling_monthly_validation, summarize_rolling,
)


def signal_row(**overrides):
    row = {
        "代码": "000001.SZ", "名称": "测试股票",
        "trend_score": 70, "direction_score": 30, "rs_score": 30,
        "breakout_score": 20, "ma_structure_score": 40, "adx_score": 60,
        "trend_stability_score": 70, "volume_score": 55,
        "stabilize_score": 50, "base_score": 40,
        "exhaustion_score": 30, "position_score": 60, "stall_score": 20,
        "ATR_ratio": 0.03,
    }
    row.update(overrides)
    return row


class OpportunityScoreTests(unittest.TestCase):
    def setUp(self):
        self.config = OpportunityConfig()

    def test_stronger_signals_receive_higher_score(self):
        strong = score_opportunity(signal_row(), config=self.config)
        weak = score_opportunity(signal_row(
            trend_score=30, direction_score=-40, rs_score=-50,
            breakout_score=-60, ma_structure_score=-50, adx_score=30,
        ), config=self.config)
        self.assertGreater(strong["机会评分"], weak["机会评分"])
        self.assertIn("主要支撑", strong["机会评分说明"])

    def test_risk_and_market_are_separate_adjustments(self):
        neutral = score_opportunity(signal_row(), config=self.config)
        risky = score_opportunity(signal_row(
            exhaustion_score=100, position_score=100, stall_score=100, ATR_ratio=.10,
        ), config=self.config)
        tailwind = score_opportunity(
            signal_row(), market_metrics={
                "trend_score": 90, "direction_score": 80, "ma_structure_score": 80,
            }, config=self.config,
        )
        self.assertGreater(risky["风险扣分"], neutral["风险扣分"])
        self.assertLess(risky["机会评分"], neutral["机会评分"])
        self.assertGreater(tailwind["大盘调整"], 0)
        self.assertGreater(tailwind["机会评分"], neutral["机会评分"])

    def test_insufficient_signals_do_not_create_false_precision(self):
        result = score_opportunity({"trend_score": 80}, config=self.config)
        self.assertTrue(pd.isna(result["机会评分"]))
        self.assertEqual(result["机会等级"], "数据不足")

    def test_output_excludes_indexes_and_sorts_descending(self):
        frame = pd.DataFrame([
            signal_row(代码="000300.SH", 名称="沪深300"),
            signal_row(代码="000688.SH", 名称="科创50"),
            signal_row(代码="000001.SZ", 名称="平安银行", trend_score=60),
            signal_row(代码="000002.SZ", 名称="万科A", trend_score=80),
        ])
        scored = add_opportunity_scores(frame, config=self.config)
        output = opportunity_output(scored)
        self.assertNotIn("000300.SH", output["代码"].tolist())
        self.assertNotIn("000688.SH", output["代码"].tolist())
        self.assertIn("000001.SZ", output["代码"].tolist())
        self.assertEqual(output.iloc[0]["代码"], "000002.SZ")

    def test_backtest_creates_cross_section_buckets_and_ic(self):
        rows = []
        for index in range(10):
            row = signal_row(
                代码=f"0000{index:02d}.SZ", trend_score=20 + index * 8,
                rs_score=-80 + index * 16,
            )
            row.update({
                "回测截面日": "2026-01-01", "样本区间": "测试期",
                "未来5日同池超额": -0.09 + index * .02,
            })
            rows.append(row)
        scored = add_scores_and_buckets(pd.DataFrame(rows), {}, self.config)
        self.assertEqual(set(scored["机会分层"]), set(["Q1偏低", "Q2", "Q3", "Q4", "Q5偏高"]))
        quality = summarize_ranking_quality(scored, [5])
        self.assertGreater(quality[quality["样本区间"] == "总体"].iloc[0]["平均秩相关IC"], 0)


class OpportunityFactorValidationTests(unittest.TestCase):
    def test_minimal_models_have_explicit_opposite_hypotheses(self):
        rows = []
        for index in range(10):
            rows.append({
                "回测截面日": "2026-01-01",
                "trend_score": index * 10,
                "rs_score": index * 10,
                "breakout_score": index * 10,
                "position_score": index * 10,
                "base_score": 90 - index * 10,
            })
        scored = add_minimal_model_scores(pd.DataFrame(rows))
        self.assertGreater(scored.iloc[-1]["趋势延续"], scored.iloc[0]["趋势延续"])
        self.assertLess(scored.iloc[-1]["回撤反弹"], scored.iloc[0]["回撤反弹"])

    def test_rolling_selection_uses_only_prior_window_and_can_abstain(self):
        rows = []
        months = pd.date_range("2024-01-01", periods=10, freq="MS")
        for index, date in enumerate(months):
            for model, ic in (("趋势延续", .10), ("回撤反弹", -.10)):
                rows.append({
                    "回测截面日": date.strftime("%Y-%m-%d"),
                    "月份": date.strftime("%Y-%m"), "样本区间": "测试",
                    "周期": "5日", "模型": model,
                    "秩相关IC": -.90 if index == 9 and model == "趋势延续" else ic,
                    "Q5减Q1同池超额": .01 if model == "趋势延续" else -.01,
                    "截面股票数": 100,
                })
        monthly = rolling_monthly_validation(
            pd.DataFrame(rows), train_months=4, min_train_snapshots=3,
        )
        last = monthly.iloc[-1]
        self.assertEqual(last["选择模型"], "趋势延续")
        self.assertGreater(last["趋势延续训练IC"], 0)
        self.assertEqual(last["趋势延续验证IC"], -.90)

        negative = pd.DataFrame(rows)
        negative["秩相关IC"] = -.10
        abstained = rolling_monthly_validation(negative, train_months=4, min_train_snapshots=3)
        self.assertTrue((abstained["选择模型"] == "不启用").all())
        summary = summarize_rolling(abstained)
        dynamic = summary[summary["验证方式"] == "月度动态选择"]
        self.assertTrue((dynamic["覆盖月份数"] == 0).all())


if __name__ == "__main__":
    unittest.main()
