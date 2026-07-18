import sys
import unittest
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from stock_industry_tags import (
    apply_tags,
    load_tag_config,
    normalize_tag,
    score_tags,
    select_output_columns,
)


class IndustryTagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_tag_config(PROJECT_ROOT / "industry_tags.toml")

    def test_alias_normalization(self):
        aliases = self.config["aliases"]
        self.assertEqual(normalize_tag("光通信模块", aliases), "光模块")
        self.assertEqual(normalize_tag("印制电路板", aliases), "PCB")
        self.assertEqual(normalize_tag("液冷概念", aliases), "液冷")

    def test_product_tags_beat_broad_themes(self):
        boards = [
            {"BOARD_RANK": 1, "BOARD_NAME": "通信", "IS_PRECISE": ""},
            {"BOARD_RANK": 2, "BOARD_NAME": "通信设备", "IS_PRECISE": "0"},
            {"BOARD_RANK": 3, "BOARD_NAME": "通信网络设备及器件", "IS_PRECISE": ""},
            {"BOARD_RANK": 21, "BOARD_NAME": "通信技术", "IS_PRECISE": "1"},
            {"BOARD_RANK": 22, "BOARD_NAME": "光通信模块", "IS_PRECISE": "1"},
            {"BOARD_RANK": 23, "BOARD_NAME": "算力概念", "IS_PRECISE": "1"},
            {"BOARD_RANK": 24, "BOARD_NAME": "CPO概念", "IS_PRECISE": "1"},
        ]
        business = [
            {
                "REPORT_DATE": "2025-12-31 00:00:00",
                "MAINOP_TYPE": "2",
                "ITEM_NAME": "光通信收发模块",
                "MBI_RATIO": 0.98,
            }
        ]
        tags = score_tags("通信设备", boards, self.config, business)
        names = [item.tag for item in tags]
        self.assertIn("光模块", names)
        self.assertNotIn("CPO", names)
        self.assertNotIn("算力", names)
        self.assertLessEqual(len(tags), 3)

    def test_duplicate_evidence_is_merged(self):
        boards = [
            {"BOARD_RANK": 1, "BOARD_NAME": "电子", "IS_PRECISE": ""},
            {"BOARD_RANK": 2, "BOARD_NAME": "元件", "IS_PRECISE": "0"},
            {"BOARD_RANK": 3, "BOARD_NAME": "印制电路板", "IS_PRECISE": ""},
            {"BOARD_RANK": 22, "BOARD_NAME": "PCB", "IS_PRECISE": "1"},
        ]
        tags = score_tags("元件", boards, self.config)
        pcb = next(item for item in tags if item.tag == "PCB")
        self.assertEqual(len(pcb.evidence), 2)
        self.assertGreaterEqual(pcb.score, 95)

    def test_index_row_is_not_tagged(self):
        stocks = pd.DataFrame(
            [{"代码": "000001.SH", "名称": "上证指数", "所属行业": "-"}]
        )
        result = apply_tags(stocks, {}, {}, self.config, tag_date="20260714")
        self.assertEqual(result.loc[0, "标签状态"], "非个股")
        self.assertEqual(result.loc[0, "标签1"], "")

    def test_latest_business_revenue_has_highest_weight(self):
        business = [
            {
                "REPORT_DATE": "2025-12-31 00:00:00",
                "MAINOP_TYPE": "2",
                "ITEM_NAME": "存储芯片",
                "MBI_RATIO": 0.71,
            },
            {
                "REPORT_DATE": "2025-12-31 00:00:00",
                "MAINOP_TYPE": "2",
                "ITEM_NAME": "微控制器",
                "MBI_RATIO": 0.21,
            },
            {
                "REPORT_DATE": "2024-12-31 00:00:00",
                "MAINOP_TYPE": "2",
                "ITEM_NAME": "旧产品",
                "MBI_RATIO": 0.90,
            },
        ]
        tags = score_tags("半导体", [], self.config, business)
        self.assertEqual(tags[0].tag, "存储")
        self.assertEqual(tags[0].score, 98)
        self.assertIn("MCU", [item.tag for item in tags])
        self.assertNotIn("旧产品", [item.tag for item in tags])

    def test_output_only_contains_identity_and_tag_fields(self):
        frame = pd.DataFrame(
            columns=["代码", "名称", "市场", "最新价", "所属行业", "标签1", "标签1相关度", "标签状态"]
        )
        self.assertEqual(
            select_output_columns(frame),
            ["代码", "名称", "市场", "标签1", "标签1相关度", "标签状态"],
        )


if __name__ == "__main__":
    unittest.main()
