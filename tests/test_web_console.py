import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from web_console import (
    JOB_LOG_DIR, TASKS, JobManager, below_ma200_preview,
    classification_count_history_preview, dashboard_status, latest_tag_file,
    latest_zsxq_audio, nearby_ma_preview, parse_progress_line, safe_task_args,
    stock_list_preview, subprocess_environment, rule_comparison_preview,
    opportunity_score_preview,
    history_coverage_preview,
    opportunity_factor_preview,
)


class WebConsoleTests(unittest.TestCase):
    def test_opportunity_score_task_and_preview_are_registered(self):
        self.assertEqual(TASKS["opportunity"].script, "generate_opportunity_scores.py")
        self.assertEqual(TASKS["opportunity_backtest"].script, "backtest_opportunity_score.py")
        preview = opportunity_score_preview()
        self.assertIn("机会评分", preview["columns"])
        self.assertGreater(preview["total"], 0)

    def test_history_backfill_tasks_and_coverage_preview_are_registered(self):
        self.assertEqual(TASKS["history_backfill"].script, "backfill_history.py")
        self.assertTrue(TASKS["history_backfill"].network)
        self.assertEqual(TASKS["history_audit"].script, "history_coverage.py")
        preview = history_coverage_preview()
        self.assertEqual(preview["total"], 303)
        self.assertIn("median_trading_days", preview["summary"])
        self.assertIn("60日非重叠截面", preview["columns"])

    def test_factor_walk_forward_task_and_preview_are_registered(self):
        task = TASKS["factor_validation"]
        self.assertEqual(task.script, "validate_opportunity_factors.py")
        self.assertIn("train_months", task.allowed)
        preview = opportunity_factor_preview()
        self.assertGreater(preview["statistics"]["months"], 0)
        self.assertIn("平均月度IC", preview["summary_columns"])
        self.assertIn("选择模型", preview["monthly_columns"])
        self.assertIn("研究候选", preview["warning"])

    def test_zsxq_task_uses_noninteractive_web_mode(self):
        task = TASKS["zsxq"]
        self.assertEqual(task.script, "拉取知识星球.py")
        self.assertTrue(task.network)
        self.assertIn("--auto-start", task.base_args)
        self.assertIn("执行当天", task.description)
        source = (PROJECT_ROOT / "src" / "拉取知识星球.py").read_text(encoding="utf-8")
        self.assertIn('default=datetime.now().strftime("%Y-%m-%d")', source)

    def test_clearance_analysis_is_available_from_workbench(self):
        task = TASKS["clearance"]
        self.assertEqual(task.script, "清仓分析.py")
        self.assertTrue(task.network)
        script = (PROJECT_ROOT / "src" / "web_ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn("'clearance'", script)

        source = (PROJECT_ROOT / "src" / "清仓分析.py").read_text(encoding="utf-8")
        self.assertIn("进度：100%", source)

    def test_tuitui_task_is_a_single_continuous_monitor(self):
        task = TASKS["tuitui"]
        self.assertEqual(task.script, "推推.py")
        self.assertEqual(task.base_args, ())
        self.assertEqual(task.allowed["interval"], ("--interval", "positive_int"))
        self.assertEqual(safe_task_args(task, {"interval": 300}), ["--interval", "300"])
        self.assertTrue(task.network)
        script = (PROJECT_ROOT / "src" / "web_ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn("'tuitui'", script)
        self.assertIn("开始持续监控", script)
        self.assertIn("终止监控", script)

        manager = JobManager()
        manager.jobs["active-monitor"] = {
            "id": "active-monitor", "task_id": "tuitui", "status": "running"
        }
        with self.assertRaisesRegex(ValueError, "已经在运行"):
            manager.start("tuitui", {"interval": 300})

    def test_tts_task_and_audio_player_are_registered(self):
        task = TASKS["tts"]
        self.assertEqual(task.script, "转语音.py")
        self.assertTrue(task.network)
        audio = latest_zsxq_audio()
        if audio:
            self.assertTrue(audio.name.endswith(".mp3"))
        self.assertIn("zsxq_audio", dashboard_status()["files"])
        script = (PROJECT_ROOT / "src" / "web_ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn("<audio controls", script)

    def test_count_cards_can_resolve_stock_lists(self):
        target = stock_list_preview("target", "强势", "2026-07-20")
        classification = stock_list_preview("classification", "上升", "2026-07-17")
        self.assertIn("代码", target["columns"])
        self.assertIn("名称", classification["columns"])
        self.assertIn("5日涨跌幅", classification["columns"])
        self.assertIn("市值(百亿)", classification["columns"])
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
        self.assertIn("5日涨跌幅", preview["columns"])
        self.assertIn("市值(百亿)", preview["columns"])
        self.assertNotIn("市值", preview["columns"])
        self.assertRegex(preview["rows"][0]["市值(百亿)"], r"^\d+\.\d{2}$")
        self.assertEqual(len(preview["rows"]), preview["total"])

    def test_nearby_ma_previews_use_latest_outputs(self):
        for period in (20, 200):
            preview = nearby_ma_preview(period)
            self.assertTrue(preview["file"]["path"])
            self.assertGreaterEqual(preview["total"], 0)
            self.assertIn("代码", preview["columns"])
            self.assertIn("名称", preview["columns"])
            self.assertIn(f"_{period}日均线附近_", preview["file"]["name"])
        with self.assertRaises(ValueError):
            nearby_ma_preview(60)

    def test_subprocess_output_is_forced_to_utf8(self):
        environment = subprocess_environment()
        self.assertEqual(environment["PYTHONUTF8"], "1")
        self.assertEqual(environment["PYTHONIOENCODING"], "utf-8")

    def test_dashboard_includes_latest_ranking_workbook(self):
        ranking = dashboard_status()["files"]["ranking"]
        expected = max(
            (PROJECT_ROOT / "data" / "input").glob("top200_stocks_*.xlsx"),
            key=lambda path: path.name,
        )
        self.assertEqual(ranking["name"], expected.name)

    def test_top200_generator_is_the_first_daily_task(self):
        task = TASKS["generate_top200"]
        self.assertEqual(task.script, "generate_top200_jqdata.py")
        self.assertTrue(task.network)
        self.assertEqual(task.allowed["date"], ("--date", "date"))
        self.assertEqual(task.allowed["limit"], ("--limit", "stock_limit"))
        self.assertEqual(
            safe_task_args(task, {"date": "2026-07-20", "limit": 5000, "skip_institutions": True}),
            ["--date", "2026-07-20", "--limit", "5000", "--skip-institutions"],
        )
        with self.assertRaises(ValueError):
            safe_task_args(task, {"date": "2026-02-30"})

    def test_dashboard_includes_latest_zsxq_output(self):
        zsxq = dashboard_status()["files"]["zsxq"]
        self.assertTrue(zsxq["name"].startswith("zsxq_"))
        self.assertTrue(zsxq["name"].endswith(".txt"))

    def test_progress_parser_supports_fraction_and_percent(self):
        self.assertEqual(parse_progress_line("[页面生成] 125/362 | 成功 124"), (125, 362))
        self.assertEqual(parse_progress_line("进度：78%"), (78, 100))
        self.assertIsNone(parse_progress_line("输出日期：2026/07/18"))

    def test_frontend_has_no_run_all_control(self):
        html = (PROJECT_ROOT / "src" / "web_ui" / "index.html").read_text(encoding="utf-8")
        script = (PROJECT_ROOT / "src" / "web_ui" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("daily-run", html)
        self.assertNotIn("dailyQueue", script)

    def test_frontend_assets_are_cache_busted(self):
        html = (PROJECT_ROOT / "src" / "web_ui" / "index.html").read_text(encoding="utf-8")
        self.assertRegex(html, r"/app\.js\?v=[0-9-]+")
        self.assertRegex(html, r"/styles\.css\?v=[0-9-]+")
        self.assertRegex(html, r"/layout\.css\?v=[0-9-]+")
        self.assertRegex(html, r"/dashboard-charts\.js\?v=[0-9-]+")

    def test_dashboard_has_offline_line_and_donut_charts(self):
        html = (PROJECT_ROOT / "src" / "web_ui" / "index.html").read_text(encoding="utf-8")
        charts = (PROJECT_ROOT / "src" / "web_ui" / "dashboard-charts.js").read_text(encoding="utf-8")
        layout = (PROJECT_ROOT / "src" / "web_ui" / "layout.css").read_text(encoding="utf-8")
        self.assertIn('id="classification-trend-chart"', html)
        self.assertIn('id="classification-donut-chart"', html)
        self.assertIn('id="target-trend-chart"', html)
        self.assertIn("renderDashboardLineChart", charts)
        self.assertIn("renderClassificationDonut", charts)
        self.assertIn("attachChartTooltips", charts)
        self.assertIn("data-chart-tooltip", charts)
        self.assertIn(".chart-tooltip", layout)
        self.assertNotIn("https://", charts)

    def test_dashboard_layout_prevents_page_level_horizontal_overflow(self):
        layout = (PROJECT_ROOT / "src" / "web_ui" / "layout.css").read_text(encoding="utf-8")
        self.assertIn("grid-template-columns: 238px minmax(0, 1fr)", layout)
        self.assertIn("overflow-x: hidden", layout)
        self.assertIn(".table-wrap", layout)
        self.assertIn("overflow: auto", layout)

    def test_task_registry_uses_python_scripts_without_shell_commands(self):
        self.assertEqual(next(iter(TASKS)), "generate_top200")
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
        self.assertEqual(
            safe_task_args(TASKS["backtest"], {"step": 60, "horizons": "60, 20,60"}),
            ["--step", "60", "--horizons", "20,60"],
        )

    def test_integer_options_have_a_bounded_range(self):
        with self.assertRaises(ValueError):
            safe_task_args(TASKS["rating"], {"workers": 1001})
        with self.assertRaises(ValueError):
            safe_task_args(TASKS["backtest"], {"step": 0})
        with self.assertRaises(ValueError):
            safe_task_args(TASKS["backtest"], {"horizons": "5;whoami"})

    def test_dashboard_never_uses_tag_audit_as_primary_output(self):
        path = latest_tag_file()
        if path:
            self.assertNotIn("审计", path.name)

    def test_rule_comparison_preview_has_stable_shape(self):
        preview = rule_comparison_preview()
        self.assertIn("available", preview)
        if preview["available"]:
            self.assertIn("performance", preview)
            self.assertIn("stability", preview)
            self.assertIn("migrations", preview)
            self.assertIn("migration_stocks", preview)
            self.assertIn("threshold_changes", preview)
            self.assertGreaterEqual(len(preview["rules"]), 2)


if __name__ == "__main__":
    unittest.main()
