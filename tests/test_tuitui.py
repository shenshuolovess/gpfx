import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from 推推 import (
    RESULT_COLUMNS, _normalize_sina_volume, append_realtime_bar,
    daily_result_path, elapsed_trading_minutes, intraday_volume_ratio,
    load_rating_engine, realtime_market_cap, write_daily_result,
)


class TuituiOutputTests(unittest.TestCase):
    def test_no_browser_or_classification_table_dependency_remains(self):
        source = (PROJECT_ROOT / "src" / "推推.py").read_text(encoding="utf-8")
        self.assertNotIn("from selenium", source)
        self.assertNotIn("CLASSIFY_FILE", source)
        self.assertNotIn("resolve_input(args.classification", source)

    def test_live_classifier_reuses_main_rating_engine(self):
        engine = load_rating_engine()
        self.assertTrue(callable(engine.analyze_one_stock_from_hist))

        dates = pd.bdate_range("2025-06-02", periods=260).strftime("%Y-%m-%d")
        stock_close = np.linspace(10, 18, len(dates))
        bench_close = np.linspace(4000, 4400, len(dates))

        def history(close):
            return pd.DataFrame({
                "date": dates,
                "open": close * 0.995,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": np.linspace(1_000_000, 1_300_000, len(dates)),
                "amount": close * np.linspace(1_000_000, 1_300_000, len(dates)),
            })

        label, _, metrics = engine.analyze_one_stock_from_hist(
            "600000", history(stock_close), history(bench_close)
        )
        self.assertIsInstance(label, str)
        self.assertIn("trend_score", metrics)

    def test_elapsed_minutes_excludes_lunch_break(self):
        self.assertEqual(elapsed_trading_minutes(datetime(2026, 7, 20, 10, 0)), 30)
        self.assertEqual(elapsed_trading_minutes(datetime(2026, 7, 20, 12, 0)), 120)
        self.assertEqual(elapsed_trading_minutes(datetime(2026, 7, 20, 14, 0)), 180)

    def test_sina_lot_volume_is_normalized_to_shares(self):
        self.assertEqual(_normalize_sina_volume(10, 1000, 1_000_000), 100_000)
        self.assertEqual(_normalize_sina_volume(10, 100_000, 1_000_000), 100_000)

    def test_intraday_volume_ratio_uses_only_completed_days(self):
        history = pd.DataFrame({
            "date": ["2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16", "2026-07-17", "2026-07-20"],
            "volume": [2400, 2400, 2400, 2400, 2400, 999999],
        })
        ratio = intraday_volume_ratio(
            history, 600, datetime(2026, 7, 20, 10, 30), "2026-07-20"
        )
        self.assertAlmostEqual(ratio, 1.0)

    def test_live_bar_projects_partial_volume_and_replaces_same_date(self):
        history = pd.DataFrame({
            "date": ["2026-07-17", "2026-07-20"],
            "open": [9, 9], "high": [10, 10], "low": [8, 8], "close": [9.5, 9.5],
            "volume": [1000, 999999], "amount": [9500, 999999],
        })
        quote = pd.Series({
            "行情日期": "2026-07-20", "最新价": 10, "昨收": 9.5,
            "今开": 9.6, "最高": 10.2, "最低": 9.4,
            "成交量": 600, "成交额": 6000,
        })
        result = append_realtime_bar(history, quote, datetime(2026, 7, 20, 10, 30))
        self.assertEqual(result["date"].tolist(), ["2026-07-17", "2026-07-20"])
        self.assertEqual(result.iloc[-1]["volume"], 2400)

    def test_market_cap_uses_live_price_and_static_share_count(self):
        value, source = realtime_market_cap(pd.Series({"总股本": "2000000000"}), 12.5)
        self.assertEqual(value, 250.0)
        self.assertIn("实时价", source)

    def test_daily_result_path_has_compact_date_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = daily_result_path(datetime(2026, 7, 20, 10, 30), directory)
            self.assertEqual(path, Path(directory) / "推推_20260720.csv")

    def test_empty_result_still_writes_stable_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_daily_result(
                pd.DataFrame(), datetime(2026, 7, 20, 10, 30), directory
            )
            result = pd.read_csv(path, encoding="utf-8-sig")
            self.assertEqual(list(result.columns), RESULT_COLUMNS)
            self.assertTrue(result.empty)

    def test_daily_result_appends_only_first_seen_stocks(self):
        with tempfile.TemporaryDirectory() as directory:
            current_time = datetime(2026, 7, 20, 10, 30)
            path = write_daily_result(pd.DataFrame({
                "代码6": ["600000", "000001"],
                "名称": ["浦发银行", "平安银行"],
                "最新价": [10.0, 11.0],
            }), current_time, directory)

            write_daily_result(pd.DataFrame({
                "代码6": ["600000.SH", "300001", "300001.SZ"],
                "名称": ["不应覆盖旧名称", "特锐德", "重复的特锐德"],
                "最新价": [99.0, 20.0, 21.0],
            }), current_time, directory)

            result = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
            self.assertEqual(result["代码6"].tolist(), ["600000", "000001", "300001"])
            self.assertEqual(result["名称"].tolist(), ["浦发银行", "平安银行", "特锐德"])
            self.assertEqual(result.iloc[0]["最新价"], "10.0")

    def test_empty_later_cycle_does_not_clear_daily_result(self):
        with tempfile.TemporaryDirectory() as directory:
            current_time = datetime(2026, 7, 20, 10, 30)
            path = write_daily_result(
                pd.DataFrame({"代码6": ["600000"], "名称": ["浦发银行"]}),
                current_time,
                directory,
            )
            before = path.read_bytes()

            write_daily_result(pd.DataFrame(), current_time, directory)

            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
