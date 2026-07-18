import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fetch_company_financials import is_index


class CompanyFinancialFetcherTests(unittest.TestCase):
    def test_known_indexes_are_excluded(self):
        self.assertTrue(is_index("000016.SH", "上证50"))
        self.assertTrue(is_index("399006.SZ", "创业板指"))
        self.assertFalse(is_index("603986.SH", "兆易创新"))


if __name__ == "__main__":
    unittest.main()
