import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from summarize_stock_research import effective_length, validate_summary


class DeepResearchSummaryTests(unittest.TestCase):
    def test_rejects_short_or_missing_report_summary(self):
        reports = [{"info_code": "A"}, {"info_code": "B"}]
        short = {"overview": "概览", "report_analyses": [{"info_code": "A", "analysis": "短" * 120}],
                 "consensus": "共识", "differences": "差异", "risks": "风险"}
        with self.assertRaises(ValueError):
            validate_summary(short, reports)

    def test_accepts_complete_eight_hundred_character_summary(self):
        reports = [{"info_code": "A"}, {"info_code": "B"}]
        payload = {
            "overview": "概" * 180,
            "report_analyses": [{"info_code": "A", "analysis": "甲" * 180},
                                {"info_code": "B", "analysis": "乙" * 180}],
            "consensus": "同" * 120, "differences": "异" * 100, "risks": "险" * 120,
        }
        result = validate_summary(payload, reports)
        self.assertGreaterEqual(effective_length(result), 800)


if __name__ == "__main__":
    unittest.main()
