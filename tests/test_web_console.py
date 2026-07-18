import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from web_console import (
    JOB_LOG_DIR, TASKS, JobManager, below_ma200_preview,
    classification_count_history_preview, dashboard_status, latest_tag_file,
    parse_progress_line, safe_task_args, stock_list_preview, subprocess_environment,
)


class WebConsoleTests(unittest.TestCase):
    def test_zsxq_task_uses_noninteractive_web_mode(self):
        task = TASKS["zsxq"]
        self.assertEqual(task.script, "拉取知识星球.py")
        self.assertTrue(task.network)
        self.assertIn("--auto-start", task.base_args)

    def test_count_cards_can_resolve_stock_lists(self):
        target = stock_list_preview("target", "强势", "2026-07-20")
        classification = stock_list_preview("classification", "上升", "2026-07-17")
        self.assertIn("代码", target["columns"])
        self.assertIn("名称", classification["columns"])
        self.assertEqual(classification["total"], 18)

    def test_stock_list_rejects_unknown_category(self):
        with self.assertRaises(ValueError):
            stock_list_preview("classification", "任意命令", "2026-07-17")

    def test_classification_history_preview_contains_nine_categories(self):
        preview = classification_count_history_preview()
        self.assertEqual(len(preview["columns"]), 10)
        self.assertGreater(preview["total"], 0)
        self.assertRegex(preview["latest"]["日期"], r"^20\d{2}-\d{2}-\d{2}$")

    def test_queued_job_can_be_cancelled_before_process_starts(self):
        manager = JobManager()
        manager.jobs["queued-test"] = {
            "id": "queued-test", "status": "queued", "logs": [],
            "cancel_requested": False, "progress_message": "等待任务启动",
        }
        result = manager.cancel("queued-test")
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["cancel_requested"])

    def test_below_ma200_preview_exposes_searchable_core_columns(self):
        preview = below_ma200_preview()
        self.assertGreater(preview["total"], 0)
        self.assertIn("代码", preview["columns"])
        self.assertIn("名称", preview["columns"])
        self.assertEqual(len(preview["rows"]), preview["total"])

    def test_subprocess_output_is_forced_to_utf8(self):
        environment = subprocess_environment()
        self.assertEqual(environment["PYTHONUTF8"], "1")
        self.assertEqual(environment["PYTHONIOENCODING"], "utf-8")

    def test_dashboard_includes_latest_ranking_workbook(self):
        ranking = dashboard_status()["files"]["ranking"]
        self.assertEqual(ranking["name"], "top200_stocks_20260720.xlsx")

    def test_progress_parser_supports_fraction_and_percent(self):
        self.assertEqual(parse_progress_line("[页面生成] 125/362 | 成功 124"), (125, 362))
        self.assertEqual(parse_progress_line("进度：78%"), (78, 100))
        self.assertIsNone(parse_progress_line("输出日期：2026/07/18"))

    def test_frontend_has_no_run_all_control(self):
        html = (PROJECT_ROOT / "src" / "web_ui" / "index.html").read_text(encoding="utf-8")
        script = (PROJECT_ROOT / "src" / "web_ui" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("daily-run", html)
        self.assertNotIn("dailyQueue", script)

    def test_task_registry_uses_python_scripts_without_shell_commands(self):
        self.assertEqual(next(iter(TASKS)), "calculate_targets")
        self.assertEqual(TASKS["calculate_targets"].script, "计算标的.py")
        self.assertIn("rating", TASKS)
        self.assertEqual(TASKS["below_ma200"].script, "低于200日(新版).py")
        self.assertEqual(TASKS["filter_ma20"].script, "filter_zd_up_ma20.py")
        self.assertEqual(TASKS["filter_ma200"].script, "filter_zd_up_ma200.py")
        self.assertTrue(all(task.script.endswith(".py") for task in TASKS.values()))
        self.assertTrue(all(";" not in task.script and "|" not in task.script for task in TASKS.values()))

    def test_only_registered_options_are_accepted(self):
        with self.assertRaises(ValueError):
            safe_task_args(TASKS["rating"], {"command": "whoami"})

    def test_integer_and_boolean_options_are_serialized_safely(self):
        self.assertEqual(safe_task_args(TASKS["rating"], {"workers": 6}), ["--workers", "6"])
        self.assertEqual(safe_task_args(TASKS["research"], {"force": True}), ["--limit", "3", "--force"])

    def test_integer_options_have_a_bounded_range(self):
        with self.assertRaises(ValueError):
            safe_task_args(TASKS["rating"], {"workers": 1001})

    def test_dashboard_never_uses_tag_audit_as_primary_output(self):
        path = latest_tag_file()
        if path:
            self.assertNotIn("审计", path.name)


if __name__ == "__main__":
    unittest.main()
