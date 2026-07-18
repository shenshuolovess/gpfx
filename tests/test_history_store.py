import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from history_store import file_sha256, history_file, load_history, merge_history
from migrate_cache_to_history import migrate


class HistoryStoreTests(unittest.TestCase):
    def test_merge_deduplicates_dates_and_keeps_new_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            merge_history(
                root,
                "600000.SH",
                pd.DataFrame({"date": ["2026-01-01", "2026-01-02"], "close": [10, 11]}),
            )
            merge_history(
                root,
                "sh.600000",
                pd.DataFrame({"date": ["2026-01-02", "2026-01-03"], "close": [12, 13]}),
            )
            result = load_history(root, "600000", verify_checksum=True)
            self.assertEqual(result["date"].tolist(), ["2026-01-01", "2026-01-02", "2026-01-03"])
            self.assertEqual(result["close"].tolist(), [10, 12, 13])

            path = history_file(root, "600000")
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            entry = manifest["securities"]["daily:sh.600000"]
            self.assertEqual(entry["rows"], 3)
            self.assertEqual(entry["sha256"], file_sha256(path))

    def test_checksum_detects_manual_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            merge_history(
                root,
                "000001.SZ",
                pd.DataFrame({"date": ["2026-01-01"], "close": [10]}),
            )
            path = history_file(root, "000001.SZ")
            path.write_text("date,close\n2026-01-01,99\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                load_history(root, "000001.SZ", verify_checksum=True)

    def test_migration_skips_header_only_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = root / "cache"
            cache.mkdir()
            (cache / "sh_000688_2025-01-01_2026-01-01_adj2.csv").write_text(
                "date,open,high,low,close,volume,amount\n", encoding="utf-8"
            )
            migrated, skipped, failed = migrate(
                cache, root / "history", benchmark_code="sh.000300"
            )
            self.assertEqual((migrated, skipped, failed), (0, 1, 0))


if __name__ == "__main__":
    unittest.main()
