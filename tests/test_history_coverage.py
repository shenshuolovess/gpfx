import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backfill_history import backfill, load_state
from history_coverage import audit_history_coverage, non_overlapping_snapshot_count
from history_store import merge_history


class HistoryCoverageTests(unittest.TestCase):
    def test_non_overlapping_snapshot_count_reserves_warmup_and_future(self):
        self.assertEqual(non_overlapping_snapshot_count(220, 5), 0)
        self.assertEqual(non_overlapping_snapshot_count(225, 5), 1)
        self.assertEqual(non_overlapping_snapshot_count(285, 60), 1)

    def test_audit_reports_coverage_lag_gaps_and_checksum(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dates = pd.bdate_range("2024-01-01", periods=300).strftime("%Y-%m-%d")
            benchmark = pd.DataFrame({"date": dates, "close": range(300)})
            stock = benchmark.drop(index=[10, 20]).copy()
            merge_history(root, "sh.000300", benchmark, kind="benchmark", adjustflag="3")
            merge_history(root, "000001.SZ", stock)
            pool = pd.DataFrame({"代码": ["000001.SZ"], "名称": ["平安银行"]})
            result = audit_history_coverage(
                pool, root, target_start="2024-01-01",
            ).iloc[0]
            self.assertEqual(result["相对基准缺口天数(含停牌)"], 2)
            self.assertEqual(result["距最新基准交易日"], 0)
            self.assertEqual(result["校验状态"], "通过")
            self.assertEqual(result["起始覆盖"], "达标")
            self.assertGreater(result["20日非重叠截面"], 0)

    def test_backfill_resumes_completed_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / "backfill_state.json"
            pool = pd.DataFrame({"代码": ["000001.SZ"], "名称": ["平安银行"]})
            frame = pd.DataFrame({
                "date": ["2021-01-04", "2021-01-05"],
                "open": [1, 1], "high": [1, 1], "low": [1, 1],
                "close": [1, 1], "volume": [1, 1], "amount": [1, 1],
            })
            with patch("backfill_history.query_daily", return_value=frame) as query:
                first = backfill(
                    pool, root, start="2021-01-01", end="2021-01-05",
                    interval=0, state_path=state_path,
                )
                second = backfill(
                    pool, root, start="2021-01-01", end="2021-01-05",
                    interval=0, state_path=state_path,
                )
            self.assertEqual(first, (2, 0, 0))
            self.assertEqual(second, (0, 2, 0))
            self.assertEqual(query.call_count, 2)
            self.assertEqual(len(load_state(state_path)["completed"]), 2)


if __name__ == "__main__":
    unittest.main()
