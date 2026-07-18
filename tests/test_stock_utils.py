import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from stock_utils import (
    dated_output_path,
    latest_matching_file,
    normalize_code,
    previous_trading_day,
    read_csv_auto,
)


class NormalizeCodeTests(unittest.TestCase):
    def test_common_exchange_formats(self):
        self.assertEqual(normalize_code("600519"), "600519")
        self.assertEqual(normalize_code("sh.600519", "suffix"), "600519.SH")
        self.assertEqual(normalize_code("600519.XSHG", "baostock"), "sh.600519")
        self.assertEqual(normalize_code("000001.SZ", "suffix"), "000001.SZ")
        self.assertEqual(normalize_code("000001.SH", "baostock"), "sh.000001")

    def test_excel_number_and_beijing_exchange(self):
        self.assertEqual(normalize_code(1.0), "000001")
        self.assertEqual(normalize_code("BJ.430047", "suffix"), "430047.BJ")


class FileUtilityTests(unittest.TestCase):
    def test_previous_trading_day_skips_weekend(self):
        self.assertEqual(previous_trading_day(date(2026, 7, 20)), date(2026, 7, 17))
        self.assertEqual(previous_trading_day(datetime(2026, 7, 18, 12, 0)), date(2026, 7, 17))
        self.assertEqual(previous_trading_day(date(2026, 7, 17)), date(2026, 7, 16))

    def test_csv_encoding_detection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "stocks.csv"
            path.write_bytes("代码,名称\n000001,平安银行\n".encode("gb18030"))
            frame = read_csv_auto(path, dtype=str)
            self.assertEqual(frame.iloc[0]["代码"], "000001")
            self.assertEqual(frame.iloc[0]["名称"], "平安银行")

    def test_latest_file_prefers_filename_date(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            older = directory / "result_20260101.csv"
            newer = directory / "result_20260201.csv"
            older.write_text("old", encoding="utf-8")
            newer.write_text("new", encoding="utf-8")
            self.assertEqual(latest_matching_file(directory, "result_*.csv"), newer)

    def test_dated_output_name(self):
        path = dated_output_path("outputs", "分类总表", date_tag="20260713")
        self.assertEqual(path, Path("outputs") / "分类总表_20260713.csv")


if __name__ == "__main__":
    unittest.main()
