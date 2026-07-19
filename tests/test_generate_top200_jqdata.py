import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from generate_top200_jqdata import (
    PREFERRED_COLUMNS, latest_completed_quarter_statdates, parse_date,
    save_outputs, score_frame, technical_metrics,
)


class Top200GeneratorTests(unittest.TestCase):
    def sample_history(self):
        dates = pd.bdate_range("2024-01-01", periods=420)
        close = pd.Series(np.linspace(10, 30, len(dates)), index=dates)
        return pd.DataFrame({
            "close": close, "high": close * 1.02, "low": close * .98,
            "volume": np.linspace(1_000_000, 2_000_000, len(dates)),
            "money": np.linspace(10_000_000, 30_000_000, len(dates)),
        }, index=dates)

    def test_date_and_quarter_are_not_hard_coded(self):
        self.assertEqual(parse_date("2026-07-20"), date(2026, 7, 20))
        self.assertEqual(latest_completed_quarter_statdates(date(2026, 7, 20)), ("2026q2", "2025q2"))
        with self.assertRaises(ValueError):
            parse_date("20260720")

    def test_technical_metrics_keep_required_downstream_fields(self):
        history = self.sample_history()
        benchmark = pd.Series(np.linspace(3000, 3600, len(history)), index=history.index)
        result = technical_metrics(history, benchmark, history.index[-1].date())
        self.assertIsNotNone(result)
        for column in [
            "当前价格", "最近15个交易日涨幅", "相对200日均线偏差(%)",
            "相对近200日高点偏差(%)", "相对历史高点跌幅", "RS线连续上涨的交易日数量(对标沪深300)",
        ]:
            self.assertIn(column, result)
            self.assertTrue(pd.notna(result[column]))

    def test_scoring_and_outputs_preserve_project_schema(self):
        rows = []
        for index in range(25):
            row = {column: np.nan for column in PREFERRED_COLUMNS}
            row.update({
                "股票代码": f"000{index:03d}.XSHE", "名称": f"测试{index}",
                "行业": "测试行业", "年初至今涨幅": index / 100,
                "200日涨幅": index / 100, "120日涨幅": index / 100,
                "60日涨幅": index / 100, "20日涨幅": index / 100,
                "10日涨幅": index / 100, "5日涨幅": index / 100,
                "近5日波动率": .03, "量比": 1 + index / 100,
                "PE": 10 + index, "PB": 1 + index / 10, "PEG": 1,
                "ROE": 5 + index, "净利润同比增长率": 10 + index,
            })
            rows.append(row)
        scored = score_frame(pd.DataFrame(rows))
        self.assertEqual(scored["RS排名"].max(), 100)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            excel, archive = save_outputs(scored[PREFERRED_COLUMNS], date(2026, 7, 20), root / "input", root / "output")
            self.assertTrue(excel.is_file())
            self.assertTrue(archive.is_file())
            loaded = pd.read_excel(excel)
            self.assertEqual(list(loaded.columns), PREFERRED_COLUMNS)


if __name__ == "__main__":
    unittest.main()
