import sys
import unittest
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from brief_renderer import data_quality_text, market_overall_summary, render_html, render_markdown
from brief_schema import deterministic_analysis, validate_analysis
from llm_client import compact_evidence_for_llm
from market_evidence import assess_group, attach_benchmark_relative, benchmark_summary, build_rankings, industry_groups, prepare_stock_frame, single_stock_influence, tag_groups


def sample_evidence():
    group = {
        "id": "industry:半导体",
        "name": "半导体",
        "kind": "industry",
        "sample_count": 5,
        "daily_return_median": 2.0,
        "up_ratio": 0.8,
        "return_5d_median": 3.0,
        "return_20d_median": 8.0,
        "return_60d_median": 10.0,
        "trend_score_median": 75.0,
        "rs_score_median": 70.0,
        "position_score_median": 60.0,
        "exhaustion_score_median": 40.0,
        "strong_state_ratio": 0.6,
        "weak_state_ratio": 0.0,
        "became_strong_count": 1,
        "became_weak_count": 0,
        "pe_median": 30.0,
        "pb_median": 3.0,
        "revenue_yoy_median": 12.0,
        "profit_yoy_median": 20.0,
        "roic_median": 8.0,
        "classification_counts": {"上升": 3},
        "positive_signals": ["趋势结构偏强", "营收与利润同步增长", "相对强弱较高"],
        "negative_signals": [],
        "positive_signal_count": 3,
        "negative_signal_count": 0,
        "signal_balance": 3,
        "board": "red",
        "leaders": [{"code": "600001.SH", "name": "甲公司", "daily_return": 3.2, "classification": "上升"}],
        "laggards": [],
        "main_stocks": [
            {"code": "600001.SH", "name": "甲公司", "market_cap": 10_000_000_000, "daily_return": 3.2, "classification": "上升", "relevance": None}
        ],
    }
    return {
        "schema_version": 1,
        "as_of": "20260716",
        "scope_note": "测试股票池",
        "sources": {"classification": "a.csv", "tags": "b.csv"},
        "market": {
            "stock_count": 5,
            "daily_return_median": 1.0,
            "up_count": 4,
            "down_count": 1,
            "flat_count": 0,
            "up_ratio": 0.8,
            "strong_state_ratio": 0.6,
            "weak_state_ratio": 0.0,
            "became_strong_count": 1,
            "became_weak_count": 0,
            "classification_counts": {"上升": 3},
        },
        "industries": [group],
        "tags": [],
        "rankings": {
            "industry_daily_leaders": [group["id"]],
            "industry_daily_laggards": [group["id"]],
            "industry_20d_leaders": [group["id"]],
            "industry_high_exhaustion": [group["id"]],
            "tag_daily_leaders": [],
            "tag_daily_laggards": [],
            "tag_20d_leaders": [],
            "tag_high_exhaustion": [],
            "industry_red_board": [group["id"]],
            "industry_black_board": [],
            "tag_red_board": [],
            "tag_black_board": [],
        },
    }


class DailyBriefTests(unittest.TestCase):
    def test_group_statistics_and_transitions_are_deterministic(self):
        classification = pd.DataFrame(
            {
                "代码": ["600001", "600002", "600003", "600004"],
                "名称": ["甲", "乙", "丙", "丁"],
                "所属行业": ["半导体"] * 4,
                "分类": ["上升", "震荡上行", "横盘", "上升"],
                "涨跌幅": ["+3%", "+1%", "-1%", "+5%"],
                "5日涨跌幅": ["5%", "3%", "-2%", "6%"],
                "20日涨跌幅": ["10%", "8%", "-3%", "12%"],
                "60日涨跌幅": ["20%", "15%", "-5%", "22%"],
                "trend_score": [80, 70, 40, 85],
                "rs_score": [90, 70, 30, 92],
                "position_score": [70, 60, 30, 75],
                "exhaustion_score": [30, 40, 20, 35],
                "市盈率TTM": [30, 40, 20, 35],
                "市净率": [3, 4, 2, 3.5],
                "市值": [100, 400, 300, 200],
            }
        )
        tags = pd.DataFrame(
            {
                "代码": ["600001", "600002", "600003", "600004"],
                "标签1": ["存储", "存储", "存储", "存储"],
                "标签1相关度": [90, 80, 70, 60],
                "标签1依据": ["a", "b", "c", "d"],
            }
        )
        previous = pd.DataFrame({"代码": ["600001", "600002", "600003", "600004"], "分类": ["横盘", "震荡上行", "横盘", "上升"]})
        frame = prepare_stock_frame(classification, tags, pd.DataFrame(), previous)
        industry = industry_groups(frame)[0]
        tag = tag_groups(frame)[0]
        self.assertEqual(industry["daily_return_median"], 2.0)
        self.assertEqual(industry["became_strong_count"], 1)
        self.assertEqual(tag["sample_count"], 4)
        self.assertEqual(tag["average_relevance"], 75.0)
        self.assertEqual(industry["financial_coverage_ratio"], 0.0)
        self.assertIn("is_high", industry["single_stock_influence"])
        self.assertEqual(industry["main_stocks"][0]["name"], "乙")
        self.assertEqual(tag["main_stocks"][0]["name"], "甲")
        self.assertEqual(build_rankings([industry], [tag])["tag_daily_leaders"], ["tag:存储"])

    def test_benchmark_is_extracted_and_relative_returns_are_attached(self):
        classification = pd.DataFrame([
            {"代码": "000300.SH", "名称": "沪深300", "分类": "过渡状态", "涨跌幅": "-1.5%", "20日涨跌幅": "-4%", "60日涨跌幅": "-2%"},
            {"代码": "000001.SH", "名称": "上证指数", "分类": "震荡下行", "涨跌幅": "-1%", "20日涨跌幅": "-3%", "60日涨跌幅": "-1%"},
        ])
        benchmark = benchmark_summary(classification)
        self.assertEqual(benchmark["primary_name"], "沪深300")
        group = {"daily_return_median": 0.5, "return_5d_median": 1.0, "return_20d_median": 2.0, "return_60d_median": 3.0}
        attach_benchmark_relative(group, benchmark)
        self.assertEqual(group["excess_daily_return"], 2.0)
        self.assertEqual(group["excess_20d_return"], 6.0)

    def test_single_stock_influence_and_quality_text(self):
        frame = pd.DataFrame({
            "代码": ["1", "2", "3", "4"], "名称": ["甲", "乙", "丙", "异常股"],
            "今日涨跌幅": [0.0, 0.2, 0.4, 10.0], "20日表现": [1.0, 1.2, 1.4, 30.0],
            "强势分类": [False, False, False, True],
        })
        result = single_stock_influence(frame)
        self.assertTrue(result["is_high"])
        self.assertEqual(result["stock_name"], "异常股")
        group = {"sample_count": 4, "financial_coverage_count": 2, "financial_coverage_ratio": 0.5,
                 "average_relevance": 88.0, "single_stock_influence": result}
        quality = data_quality_text(group)
        self.assertIn("财务覆盖2/4（50.0%）", quality)
        self.assertIn("标签平均相关度88.0分", quality)
        self.assertIn("异常股", quality)

    def test_model_output_must_reference_known_evidence_and_not_invent_numbers(self):
        evidence = sample_evidence()
        valid = deterministic_analysis(evidence)
        self.assertEqual(valid["industry_view"][0]["evidence_ids"], ["industry:半导体"])
        with self.assertRaises(ValueError):
            validate_analysis(
                {
                    "market_summary": "上涨百分之五。",
                    "industry_view": [{"evidence_ids": ["industry:不存在"], "interpretation": "明显走强", "caveat": ""}],
                },
                evidence,
            )
        with self.assertRaises(ValueError):
            validate_analysis(
                {
                    "market_summary": "市场结构分化。",
                    "industry_view": [], "tag_view": [], "turning_points": [], "risks": [],
                },
                evidence,
            )
        with self.assertRaises(ValueError):
            validate_analysis(
                {
                    "market_summary": "市场上涨5%。",
                    "industry_view": [], "tag_view": [], "turning_points": [], "risks": [],
                },
                evidence,
            )

    def test_red_and_black_boards_use_multiple_transparent_signals(self):
        market = {"daily_return_median": 0.0}
        red = {
            "id": "industry:红榜样本", "name": "红榜样本", "sample_count": 8,
            "daily_return_median": 2.0, "up_ratio": 0.75,
            "return_20d_median": 8.0, "return_60d_median": 12.0,
            "trend_score_median": 70.0, "strong_state_ratio": 0.6,
            "rs_score_median": 70.0, "became_strong_count": 2, "became_weak_count": 0,
            "revenue_yoy_median": 10.0, "profit_yoy_median": 15.0,
            "roic_median": 10.0, "position_score_median": 60.0,
            "exhaustion_score_median": 30.0, "pe_median": 30.0,
        }
        black = {
            "id": "industry:黑榜样本", "name": "黑榜样本", "sample_count": 8,
            "daily_return_median": -3.0, "up_ratio": 0.1,
            "return_20d_median": -8.0, "return_60d_median": -12.0,
            "trend_score_median": 35.0, "strong_state_ratio": 0.1,
            "rs_score_median": 25.0, "became_strong_count": 0, "became_weak_count": 2,
            "revenue_yoy_median": -10.0, "profit_yoy_median": -15.0,
            "roic_median": 2.0, "position_score_median": 75.0,
            "exhaustion_score_median": 80.0, "pe_median": 120.0,
        }
        assess_group(red, market)
        assess_group(black, market)
        self.assertEqual(red["board"], "red")
        self.assertIn("营收与利润同步增长", red["positive_signals"])
        self.assertEqual(black["board"], "black")
        self.assertIn("高位衰竭风险偏高", black["negative_signals"])
        rankings = build_rankings([red, black], [])
        self.assertEqual(rankings["industry_red_board"], ["industry:红榜样本"])
        self.assertEqual(rankings["industry_black_board"], ["industry:黑榜样本"])

    def test_llm_view_drops_tiny_groups_and_renderer_injects_numbers(self):
        evidence = sample_evidence()
        tiny = dict(evidence["industries"][0], id="industry:三只样本", name="三只样本", sample_count=3)
        evidence["industries"].append(tiny)
        compact = compact_evidence_for_llm(evidence)
        self.assertEqual([item["id"] for item in compact["industries"]], ["industry:半导体"])
        report = render_markdown(evidence, deterministic_analysis(evidence), "测试模式")
        self.assertIn("红榜：多维证据偏强", report)
        self.assertIn("趋势结构偏强、营收与利润同步增长", report)
        self.assertIn("今日中位 +2.00%", report)
        self.assertIn("主要股票：甲公司(600001.SH；市值100.0亿", report)
        self.assertIn("## 市场整体总结", report)
        self.assertIn("上涨比例为80.0%", report)
        self.assertIn("行业红榜主要为半导体", report)
        self.assertIn("数据质量：", report)
        html_report = render_html(evidence, deterministic_analysis(evidence), "测试模式")
        self.assertIn("<h2>市场整体总结</h2>", html_report)
        self.assertIn("<h2>大盘基准</h2>", html_report)
        self.assertIn('<details class="stock-details"><summary>展开股票明细</summary>', html_report)
        self.assertIn('<div class="stock-row">甲公司(600001.SH；市值100.0亿', html_report)
        self.assertIn('<h4>当日领涨</h4><div class="stock-row">甲公司（+3.20%）</div>', html_report)
        self.assertNotIn("<span>主要股票：", html_report)
        self.assertIn("分类状态呈现净转强", market_overall_summary(evidence))
        self.assertIn("数字、排名和代表股票均由代码生成", report)


if __name__ == "__main__":
    unittest.main()
