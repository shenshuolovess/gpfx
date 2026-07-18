import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from logging_utils import get_rotating_logger
from maintenance import (
    find_baostock_candidates,
    find_generic_cache_candidates,
    main as maintenance_main,
)


class LoggingTests(unittest.TestCase):
    def test_rotating_file_handler_is_installed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "app.log"
            logger = get_rotating_logger("rotation-test", log_file, console=False)
            self.assertTrue(any(isinstance(item, RotatingFileHandler) for item in logger.handlers))
            logger.info("hello")
            for handler in logger.handlers:
                handler.flush()
                handler.close()
            logger.handlers.clear()
            self.assertIn("hello", log_file.read_text(encoding="utf-8"))


class MaintenanceTests(unittest.TestCase):
    def _old_file(self, path: Path):
        path.write_text("date,close\n2026-01-01,1\n", encoding="utf-8")
        old_time = (datetime.now() - timedelta(days=60)).timestamp()
        os.utime(path, (old_time, old_time))

    def test_cache_retention_keeps_latest_per_code(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = Path(temp_dir)
            for end_date in ("2026-01-01", "2026-02-01", "2026-03-01"):
                self._old_file(cache / f"sh_600000_2025-01-01_{end_date}_adj2.csv")
            candidates = find_baostock_candidates(
                cache,
                keep_per_code=1,
                cutoff=datetime.now() - timedelta(days=30),
            )
            self.assertEqual(len(candidates), 2)
            self.assertNotIn("2026-03-01", {item.path.name for item in candidates})

    def test_default_mode_is_dry_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = root / "cache"
            baostock = cache / "baostock"
            output = root / "output"
            baostock.mkdir(parents=True)
            output.mkdir()
            old_file = baostock / "sh_600000_2025-01-01_2026-01-01_adj2.csv"
            newer_file = baostock / "sh_600000_2025-01-01_2026-02-01_adj2.csv"
            self._old_file(old_file)
            self._old_file(newer_file)
            result = maintenance_main(
                [
                    "--cache-root", str(cache),
                    "--baostock-dir", str(baostock),
                    "--output-dir", str(output),
                    "--cache-days", "0",
                    "--keep-per-code", "1",
                ]
            )
            self.assertEqual(result, 0)
            self.assertTrue(old_file.exists())

    def test_history_directory_is_never_generic_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            history = root / "data" / "history"
            history.mkdir(parents=True)
            history_file = history / "600000.csv"
            self._old_file(history_file)
            candidates = find_generic_cache_candidates(
                root,
                baostock_dir=root / "cache" / "baostock",
                cutoff=datetime.now(),
                protected_roots=(history,),
            )
            self.assertNotIn(history_file.resolve(), {item.path.resolve() for item in candidates})


if __name__ == "__main__":
    unittest.main()
