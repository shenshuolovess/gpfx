import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fetch_stock_research import extract_detail


class ResearchReportTests(unittest.TestCase):
    def test_extracts_short_viewpoint_headings_and_risk(self):
        page = '''<html><div id="ctx-content"><p>测试股份(600001)</p>
        <p>投资要点</p><p>盈利恢复，维持买入评级</p><p>这是很长的正文说明，包含详细的业务数据和预测内容。</p>
        <p>新产品进入放量阶段</p><p>风险提示：需求不及预期。</p></div></html>'''
        result = extract_detail(page, "备用标题")
        self.assertEqual(result["viewpoints"], ["盈利恢复，维持买入评级", "新产品进入放量阶段"])
        self.assertEqual(result["risk"], "风险提示：需求不及预期。")

    def test_title_is_fallback_when_detail_has_no_viewpoint(self):
        result = extract_detail('<div id="ctx-content"><p>测试股份(600001)</p></div>', "备用标题")
        self.assertEqual(result["viewpoints"], ["备用标题"])


if __name__ == "__main__":
    unittest.main()
