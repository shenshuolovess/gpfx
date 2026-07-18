import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

spec = importlib.util.spec_from_file_location("calculate_targets", PROJECT_ROOT / "src" / "计算标的.py")
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


class CalculateTargetHistoryTests(unittest.TestCase):
    def test_same_date_is_replaced_and_dates_are_sorted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "counts.csv"
            module.update_count_history(path, "20260720", {"强势": 3, "近期新高": 4, "历史新高": 1})
            module.update_count_history(path, "20260718", {"强势": 2, "近期新高": 2, "历史新高": 0})
            frame = module.update_count_history(path, "20260720", {"强势": 5, "近期新高": 6, "历史新高": 2})
            self.assertEqual(frame["日期"].tolist(), ["2026-07-18", "2026-07-20"])
            self.assertEqual(int(frame.iloc[-1]["强势数量"]), 5)
            self.assertEqual(len(frame), 2)


if __name__ == "__main__":
    unittest.main()
