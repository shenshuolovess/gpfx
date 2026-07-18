import sys
import unittest
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from generate_stock_page import (
    build_research_comparison,
    build_price_series,
    build_risk_notes,
    build_fundamental_notes,
    build_minimal_analysis,
    large_number_text,
    latest_business_mix,
    latest_company_financial,
    is_non_stock,
    render_index_page,
    render_page,
    safe_company_filename,
    scalar_text,
)


class StockPageTests(unittest.TestCase):
    def test_company_filename_is_windows_safe(self):
        self.assertEqual(
            safe_company_filename("测试/股份:*?", "600000.SH"),
            "测试_股份___.html",
        )
        self.assertEqual(safe_company_filename("CON", "600000.SH"), "CON_股票.html")

    def test_nan_is_not_rendered_as_text(self):
        self.assertEqual(scalar_text(float("nan")), "")

    def test_research_comparison_detects_consensus_topics_and_dispersion(self):
        reports = [
            {"publish_date": "2026-07-15", "rating": "买入", "title": "业绩增长与产品放量", "viewpoints": ["利润高增"], "risk": "行业竞争加剧", "forecasts": [{"year": 2026, "eps": 1.00, "pe": 20}]},
            {"publish_date": "2026-07-10", "rating": "买入", "title": "业绩超预期", "viewpoints": ["营收增长"], "risk": "行业竞争加剧", "forecasts": [{"year": 2026, "eps": 1.08, "pe": 19}]},
            {"publish_date": "2026-07-01", "rating": "买入", "title": "利润增长", "viewpoints": ["新品放量"], "risk": "需求不及预期", "forecasts": [{"year": 2026, "eps": 1.04, "pe": 18}]},
        ]
        result = build_research_comparison(reports, "2026-07-15")
        self.assertEqual(result["coverage_count"], 3)
        self.assertEqual(result["rating_consensus"], "评级一致")
        self.assertEqual(result["focuses"][0], {"name": "业绩增长", "count": 3})
        self.assertEqual(result["risks"][0], {"name": "行业竞争", "count": 2})
        self.assertEqual(result["dispersion_level"], "分歧较小")

    def test_index_page_links_company_named_files(self):
        page = render_index_page(
            [
                {
                    "name": "兆易创新",
                    "code": "603986.SH",
                    "market": "上海",
                    "classification": "上升",
                    "tag": "存储",
                    "filename": "兆易创新.html",
                    "search": "兆易创新 603986.SH 上升 存储",
                }
            ],
            "2026-07-15T12:00:00+08:00",
        )
        self.assertIn('href="兆易创新.html"', page)
        self.assertIn("共 1 只个股", page)

    def test_known_index_is_skipped_even_if_tag_status_is_wrong(self):
        row = pd.Series({"代码": "000016.SH", "名称": "上证50"})
        tag_row = pd.Series({"标签状态": "已完成", "标签1": "冰洗"})
        self.assertTrue(is_non_stock(row, tag_row))

    def test_large_number_is_rendered_in_yi(self):
        self.assertEqual(large_number_text("401250792991"), "4,012.5 亿")

    def test_price_series_contains_moving_averages(self):
        history = pd.DataFrame(
            {
                "date": [f"2026-01-{day:02d}" for day in range(1, 21)],
                "close": list(range(1, 21)),
                "volume": [100] * 20,
            }
        )
        result = build_price_series(history)
        self.assertEqual(result[-1]["ma20"], 10.5)
        self.assertIsNone(result[-1]["ma60"])

    def test_business_mix_uses_latest_product_report(self):
        payload = {
            "business_profiles": {
                "603986.SH": [
                    {"REPORT_DATE": "2024-12-31", "MAINOP_TYPE": "2", "ITEM_NAME": "旧产品", "MBI_RATIO": 1},
                    {"REPORT_DATE": "2025-12-31", "MAINOP_TYPE": "2", "ITEM_NAME": "存储芯片", "MBI_RATIO": 0.71},
                    {"REPORT_DATE": "2025-12-31", "MAINOP_TYPE": "2", "ITEM_NAME": "其他", "MBI_RATIO": 0.29},
                ]
            }
        }
        result = latest_business_mix(payload, "603986.SH")
        self.assertEqual(result[0]["name"], "存储芯片")
        self.assertNotIn("旧产品", [item["name"] for item in result])

    def test_latest_company_financial_uses_latest_report(self):
        payload = {
            "records": {
                "603986.SH": {
                    "company": {"REG_ADDRESS": "北京市海淀区"},
                    "listing": {"FOUND_DATE": "2005-04-06"},
                    "financials": [
                        {"REPORT_DATE": "2025-12-31", "EPSJB": 1.0},
                        {"REPORT_DATE": "2026-03-31", "EPSJB": 2.19},
                    ],
                }
            }
        }
        company, listing, financial = latest_company_financial(payload, "603986")
        self.assertEqual(company["REG_ADDRESS"], "北京市海淀区")
        self.assertEqual(listing["FOUND_DATE"], "2005-04-06")
        self.assertEqual(financial["EPSJB"], 2.19)

    def test_fundamental_notes_detect_growth_and_cash_quality(self):
        notes = build_fundamental_notes(
            {
                "TOTALOPERATEREVETZ": 10,
                "PARENTNETPROFITTZ": 20,
                "MGJYXJJE": -0.3,
                "ZCFZL": 75,
            }
        )
        self.assertEqual(notes[0]["title"], "收入与利润同步增长")
        self.assertEqual(notes[1]["title"], "经营现金流为负")
        self.assertEqual(notes[2]["level"], "warn")

    def test_minimal_analysis_keeps_four_independent_pillars(self):
        stock = pd.Series({"所属行业": "半导体", "市盈率TTM": "20", "市净率": "3"})
        classification = pd.Series(
            {"分类": "上升", "trend_score": 80, "rs_score": 70, "position_score": 60, "exhaustion_score": 40}
        )
        tags = pd.Series({"标签1": "存储", "标签2": "MCU"})
        pool = pd.DataFrame(
            {
                "所属行业": ["半导体"] * 5,
                "市盈率TTM": [10, 15, 20, 30, 40],
            }
        )
        result = build_minimal_analysis(
            stock,
            classification,
            tags,
            [{"name": "存储芯片", "ratio": 0.7}],
            {
                "TOTALOPERATEREVETZ": 10,
                "PARENTNETPROFITTZ": 20,
                "MGJYXJJE": 1,
                "ROIC": 8,
                "ZCFZL": 30,
            },
            pool,
        )
        self.assertEqual([item["name"] for item in result["pillars"]], ["业务", "盈利", "估值", "市场"])
        self.assertEqual(result["pillars"][1]["state"], "盈利改善")
        self.assertEqual(result["risk_gate"], "未触发核心否决项")
        self.assertIn("5个有效样本", result["pillars"][2]["evidence"])

    def test_high_risk_signals_are_explained(self):
        row = pd.Series(
            {
                "exhaustion_score": 80,
                "ATR_ratio": 0.1,
                "price_ma20_dev": -0.12,
                "price_ma200_dev": 0.8,
                "市盈率TTM": 100,
            }
        )
        notes = build_risk_notes(row)
        self.assertEqual(len(notes), 5)
        self.assertIn("衰竭风险偏高", [item["title"] for item in notes])

    def test_rendered_page_is_offline_and_contains_sections(self):
        stock = pd.Series(
            {
                "名称": "测试股份",
                "代码": "600000.SH",
                "市场": "上海",
                "所属行业": "测试行业",
                "最新价": "10.00",
                "涨跌幅": "+1.00%",
                "市值": "1000000000",
                "市盈率TTM": "20",
                "市净率": "2",
                "换手率": "1%",
                "振幅": "2%",
                "20日涨跌幅": "+3%",
                "60日涨跌幅": "+8%",
            }
        )
        classification = pd.Series(
            {
                "分类": "上升",
                "截止交易日": "2026-07-15",
                "trend_score": 80,
                "direction_score": 70,
                "trend_stability_score": 60,
                "adx_score": 70,
                "rs_score": 75,
                "position_score": 55,
            }
        )
        tags = pd.Series(
            {"标签1": "存储", "标签1相关度": "95", "标签1依据": "主营产品"}
        )
        page = render_page(
            {
                "stock": stock,
                "classification": classification,
                "tags": tags,
                "price_series": [{"date": "2026-07-15", "close": 10, "volume": 1, "ma20": 9, "ma60": 8, "ma200": 7}],
                "business_mix": [{"name": "存储芯片", "ratio": 0.8, "date": "2025-12-31"}],
                "risks": [{"level": "ok", "title": "测试", "text": "测试说明"}],
                "research_reports": [{
                    "publish_date": "2026-07-14", "organization": "测试证券", "rating": "买入",
                    "rating_change_name": "维持",
                    "title": "业绩增长与产品放量", "viewpoints": ["主营产品需求增长", "盈利能力改善"],
                    "risk": "风险提示：需求不及预期。",
                    "forecasts": [{"year": 2026, "eps": 1.23, "pe": 20.5}],
                }],
                "summary": "测试摘要。",
                "generated_date": "2026-07-15",
                "generated_at": "2026-07-15T00:00:00+08:00",
            }
        )
        self.assertIn("测试股份", page)
        self.assertIn("价格趋势", page)
        self.assertIn("主营收入构成", page)
        self.assertIn("四维核心判断", page)
        self.assertIn("展开完整分析证据", page)
        self.assertIn("最新研报观点", page)
        self.assertIn("最新三份研报对比", page)
        self.assertIn("盈利预测对照", page)
        self.assertIn("评级一致", page)
        self.assertNotIn("深度总结尚未生成", page)
        self.assertIn("主营产品需求增长", page)
        self.assertNotIn("https://", page)


if __name__ == "__main__":
    unittest.main()
