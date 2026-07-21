import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

spec = importlib.util.spec_from_file_location(
    "rating_count_history", PROJECT_ROOT / "src" / "综合评级_安全缓存并发版(1).py"
)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


class RatingCountHistoryTests(unittest.TestCase):
    def test_benchmark_fetch_requests_complete_daily_bars(self):
        expected = pd.DataFrame({
            "date": ["2026-07-20"],
            "open": [4000],
            "high": [4050],
            "low": [3980],
            "close": [4030],
            "volume": [1000000],
            "amount": [2000000],
        })
        with patch.object(module, "fetch_bs_data", return_value=expected) as fetch:
            result = module.fetch_benchmark_data("2025-01-01", "2026-07-20")

        self.assertIs(result, expected)
        fetch.assert_called_once_with(
            module.BENCHMARK_CODE,
            module.DAILY_BAR_FIELDS,
            "2025-01-01",
            "2026-07-20",
            adjustflag="3",
        )

    def test_benchmark_fetch_rejects_incomplete_daily_bars(self):
        with patch.object(
            module,
            "fetch_bs_data",
            return_value=pd.DataFrame({"date": ["2026-07-20"], "close": [4030]}),
        ):
            with self.assertRaisesRegex(RuntimeError, "缺少必要字段"):
                module.fetch_benchmark_data("2025-01-01", "2026-07-20")

    def test_nine_categories_are_written_and_same_date_is_replaced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "classification_counts.csv"
            first = {name: index for index, name in enumerate(module.CATEGORIES, start=1)}
            module.update_classification_count_history(path, "20260720", first)
            second = {name: index + 10 for index, name in enumerate(module.CATEGORIES, start=1)}
            frame = module.update_classification_count_history(path, "20260720", second)
            self.assertEqual(len(frame), 1)
            self.assertEqual(len(frame.columns), 10)
            self.assertEqual(int(frame.iloc[0]["上升数量"]), 11)
            self.assertEqual(int(frame.iloc[0]["边界模糊数量"]), 19)


if __name__ == "__main__":
    unittest.main()
