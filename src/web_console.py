"""本地 Web 控制台：以白名单方式运行项目任务并浏览结果。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pipeline_config import PROJECT_DIR, config_value, project_path, resolve_input
from stock_utils import latest_matching_file, read_csv_auto


STATIC_DIR = Path(__file__).with_name("web_ui")
JOB_LOG_DIR = PROJECT_DIR / "data" / "output" / "logs" / "web_console_jobs"
OUTPUT_DIR = PROJECT_DIR / "data" / "output"
CLASSIFICATION_NAMES = [
    "上升", "震荡上行", "横盘", "震荡下行", "下降",
    "筑底", "赶顶", "过渡状态", "边界模糊",
]
TARGET_NAMES = ["强势", "近期新高", "历史新高"]


@dataclass(frozen=True)
class TaskDefinition:
    title: str
    description: str
    script: str
    category: str
    network: bool = False
    base_args: tuple[str, ...] = ()
    allowed: dict[str, tuple[str, str]] = field(default_factory=dict)


TASKS: dict[str, TaskDefinition] = {
    "calculate_targets": TaskDefinition("计算标的", "从最新选股明细生成强势、近期新高和历史新高三类标的", "计算标的.py", "每日准备"),
    "zsxq": TaskDefinition("拉取知识星球", "打开浏览器，登录后自动抓取上一个交易日的文字观点", "拉取知识星球.py", "每日准备", True, base_args=("--auto-start",)),
    "tts": TaskDefinition("知识星球转语音", "自动选择最新知识星球文本，生成可在页面播放的语音", "转语音.py", "每日准备", True),
    "below_ma200": TaskDefinition("低于200日线", "读取最新选股明细，筛出位于200日均线下方的股票", "低于200日(新版).py", "每日准备"),
    "rating": TaskDefinition("综合评级", "更新行情并生成最新分类总表", "综合评级_安全缓存并发版(1).py", "核心分析", True, allowed={"workers": ("--workers", "int")}),
    "filter_ma20": TaskDefinition("20日均线附近", "筛选震荡上行、上升、赶顶且位于20日均线附近的股票", "filter_zd_up_ma20.py", "每日筛选"),
    "filter_ma200": TaskDefinition("200日均线附近", "筛选震荡上行、上升、赶顶且位于200日均线上方附近的股票", "filter_zd_up_ma200.py", "每日筛选"),
    "tags": TaskDefinition("产业标签", "生成每只股票最多三个细分产业标签", "stock_industry_tags.py", "核心分析", allowed={"offline": ("--offline", "bool"), "refresh": ("--refresh", "bool")}),
    "daily_brief": TaskDefinition("市场日报", "生成行业与标签红黑榜及市场总结", "daily_market_brief.py", "结果生成", base_args=("--no-llm",)),
    "stock_pages": TaskDefinition("股票页面", "批量生成全部股票专属研究页面", "generate_stock_page.py", "结果生成", base_args=("--all",)),
    "company_data": TaskDefinition("公司财务", "更新公司概况与最新财务快照", "fetch_company_financials.py", "数据更新", True, allowed={"workers": ("--workers", "int"), "force": ("--force", "bool")}),
    "research": TaskDefinition("最新研报", "更新最近三份公开研报并复用已有正文", "fetch_stock_research.py", "数据更新", True, base_args=("--limit", "3"), allowed={"workers": ("--workers", "int"), "force": ("--force", "bool")}),
    "backtest": TaskDefinition("分类回测", "运行分类边界的稳健历史回测", "backtest_classification.py", "规则验证", allowed={"max_stocks": ("--max-stocks", "int"), "snapshots": ("--snapshots", "int"), "step": ("--step", "positive_int"), "horizons": ("--horizons", "horizons")}),
    "compare_rules": TaskDefinition("规则对比", "比较当前基线与候选分类规则", "compare_classification_rules.py", "规则验证", allowed={"max_stocks": ("--max-stocks", "int"), "snapshots": ("--snapshots", "int"), "step": ("--step", "positive_int"), "horizons": ("--horizons", "horizons")}),
    "opportunity": TaskDefinition("机会评分", "从最新分类总表生成透明机会评分榜单", "generate_opportunity_scores.py", "结果生成"),
    "opportunity_backtest": TaskDefinition("机会评分回测", "检验机会评分的同池分层排序能力", "backtest_opportunity_score.py", "规则验证", allowed={"max_stocks": ("--max-stocks", "int"), "snapshots": ("--snapshots", "int"), "step": ("--step", "positive_int"), "horizons": ("--horizons", "horizons")}),
    "factor_validation": TaskDefinition("机会因子滚动验证", "比较趋势延续与回撤反弹，并按月只使用已知历史选择模型", "validate_opportunity_factors.py", "规则验证", allowed={"train_months": ("--train-months", "positive_int"), "min_train_snapshots": ("--min-train-snapshots", "positive_int"), "horizons": ("--horizons", "horizons")}),
    "history_backfill": TaskDefinition("历史数据补全", "从2021年起补全正式历史行情，支持断点续跑", "backfill_history.py", "数据更新", True, allowed={"force": ("--force", "bool")}),
    "history_audit": TaskDefinition("历史覆盖审计", "检查历史起止日期、缺口、校验和与回测可用截面", "history_coverage.py", "规则验证"),
    "maintenance": TaskDefinition("维护预览", "预览过期缓存和日志，不执行删除", "maintenance.py", "系统维护"),
}

PROGRESS_PATTERNS = (
    re.compile(r"(?<!\d)(\d{1,6})\s*/\s*(\d{1,6})(?!\d)"),
    re.compile(r"进度[：:]?\s*(\d{1,3})%"),
)


class TaskRequest(BaseModel):
    options: dict[str, Any] = Field(default_factory=dict)


def safe_task_args(task: TaskDefinition, options: dict[str, Any]) -> list[str]:
    unknown = set(options) - set(task.allowed)
    if unknown:
        raise ValueError(f"不支持的参数：{', '.join(sorted(unknown))}")
    result = list(task.base_args)
    for name, value in options.items():
        flag, kind = task.allowed[name]
        if kind == "bool":
            if bool(value):
                result.append(flag)
        elif kind in {"int", "positive_int"}:
            number = int(value)
            minimum = 1 if kind == "positive_int" else 0
            if number < minimum or number > 1000:
                raise ValueError(f"{name} 超出允许范围 {minimum}—1000")
            result.extend([flag, str(number)])
        elif kind == "horizons":
            text = str(value or "").strip()
            if not re.fullmatch(r"\d+(?:\s*,\s*\d+)*", text):
                raise ValueError("horizons 必须是逗号分隔的正整数")
            horizons = sorted({int(item.strip()) for item in text.split(",")})
            if not horizons or any(item <= 0 or item > 1000 for item in horizons):
                raise ValueError("horizons 必须位于1—1000")
            result.extend([flag, ",".join(map(str, horizons))])
    return result


def parse_progress_line(line: str) -> tuple[int, int] | None:
    fraction = PROGRESS_PATTERNS[0].search(line)
    if fraction:
        current, total = map(int, fraction.groups())
        if total > 0 and 0 <= current <= total:
            return current, total
    percent = PROGRESS_PATTERNS[1].search(line)
    if percent:
        value = min(100, int(percent.group(1)))
        return value, 100
    return None


def update_progress(job: dict[str, Any], line: str) -> None:
    parsed = parse_progress_line(line)
    job["progress_message"] = line[-160:]
    if parsed:
        current, total = parsed
        if job.get("progress_total") == total:
            current = max(current, int(job.get("progress_current") or 0))
        job.update(
            progress_current=current,
            progress_total=total,
            progress=round(current / total * 100, 1),
            progress_measurable=True,
        )


def subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, check=False,
        )
    else:
        process.terminate()


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.processes: dict[str, subprocess.Popen[str]] = {}
        self.lock = threading.Lock()
        JOB_LOG_DIR.mkdir(parents=True, exist_ok=True)

    def start(self, task_id: str, options: dict[str, Any]) -> dict[str, Any]:
        task = TASKS[task_id]
        args = safe_task_args(task, options)
        job_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
        command = [sys.executable, "-u", str(PROJECT_DIR / "src" / task.script), *args]
        job = {
            "id": job_id, "task_id": task_id, "title": task.title,
            "status": "queued", "created_at": now_text(), "started_at": "",
            "finished_at": "", "return_code": None, "options": options,
            "command": [Path(command[0]).name, task.script, *args], "logs": deque(maxlen=500),
            "progress": 0.0, "progress_current": 0, "progress_total": 0,
            "progress_measurable": False, "progress_message": "等待任务启动",
            "cancel_requested": False,
        }
        with self.lock:
            self.jobs[job_id] = job
        threading.Thread(target=self._run, args=(job_id, command), daemon=True).start()
        return self.public(job)

    def _run(self, job_id: str, command: list[str]) -> None:
        job = self.jobs[job_id]
        log_path = JOB_LOG_DIR / f"{job_id}.log"
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            if job["cancel_requested"]:
                return
            job.update(status="running", started_at=now_text(), progress_message="任务已启动")
            process = subprocess.Popen(
                command, cwd=PROJECT_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=creationflags, env=subprocess_environment(),
            )
            with self.lock:
                self.processes[job_id] = process
            if job["cancel_requested"]:
                terminate_process_tree(process)
            with log_path.open("w", encoding="utf-8") as output:
                assert process.stdout is not None
                for line in process.stdout:
                    line = line.rstrip()
                    job["logs"].append(line)
                    update_progress(job, line)
                    output.write(line + "\n")
                    output.flush()
            code = process.wait()
            if job["status"] != "cancelled":
                if code == 0:
                    job.update(
                        status="success", return_code=code, progress=100.0,
                        progress_measurable=True, progress_message="任务执行完成",
                    )
                else:
                    job.update(status="failed", return_code=code, progress_message="任务执行失败")
            else:
                job["return_code"] = code
        except Exception as exc:
            job["logs"].append(f"控制台运行失败：{exc}")
            job.update(status="failed", return_code=-1, progress_message="控制台运行失败")
        finally:
            job["finished_at"] = now_text()
            with self.lock:
                self.processes.pop(job_id, None)

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        if job["status"] not in {"queued", "running"}:
            return self.public(job)
        job["cancel_requested"] = True
        process = self.processes.get(job_id)
        if process and process.poll() is None:
            terminate_process_tree(process)
        job["logs"].append("任务已由用户终止。")
        job.update(status="cancelled", finished_at=now_text(), progress_message="任务已终止")
        return self.public(job)

    @staticmethod
    def public(job: dict[str, Any]) -> dict[str, Any]:
        result = dict(job)
        result["logs"] = list(job["logs"])
        return result

    def list(self) -> list[dict[str, Any]]:
        return [self.public(job) for job in reversed(list(self.jobs.values()))]


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def latest_optional(pattern: str) -> Path | None:
    try:
        return latest_matching_file(PROJECT_DIR, pattern)
    except FileNotFoundError:
        return None


def latest_tag_file() -> Path | None:
    candidates = [
        path for path in OUTPUT_DIR.glob("沪深_产业标签_*.csv")
        if "审计" not in path.name
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime_ns) if candidates else None


def latest_zsxq_audio() -> Path | None:
    return latest_optional("data/output/output_full_*_zsxq_*.mp3")


def file_info(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {"name": "尚未生成", "path": "", "modified": "", "size_mb": 0}
    return {
        "name": path.name,
        "path": str(path.relative_to(PROJECT_DIR)).replace("\\", "/"),
        "modified": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="minutes"),
        "size_mb": round(path.stat().st_size / 1024 / 1024, 2),
    }


def json_record_coverage(path: Path | None) -> tuple[int, int]:
    if not path:
        return 0, 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload.get("records", {})
        covered = sum(bool(item.get("reports")) for item in records.values())
        return covered, len(records)
    except (OSError, json.JSONDecodeError):
        return 0, 0


def dashboard_status() -> dict[str, Any]:
    pool_path = resolve_input(None, config_key="stock_pool")
    ranking_path = resolve_input(None, pattern_key="ranking_pattern")
    classification = latest_optional(str(config_value("files", "classification_pattern")))
    tags = latest_tag_file()
    zsxq = latest_optional("data/output/zsxq_*.txt")
    zsxq_audio = latest_zsxq_audio()
    research = latest_optional("data/history/research_reports/eastmoney_stock_reports_*.json")
    financial = latest_optional("data/history/company_financials/eastmoney_company_financials_*.json")
    try:
        pool_count = len(read_csv_auto(pool_path, dtype=str))
    except Exception:
        pool_count = 0
    report_covered, report_total = json_record_coverage(research)
    stock_pages = list((OUTPUT_DIR / "stock_pages").glob("*.html"))
    page_count = max(0, len(stock_pages) - int((OUTPUT_DIR / "stock_pages" / "index.html").exists()))
    return {
        "generated_at": now_text(), "pool_count": pool_count, "page_count": page_count,
        "research_covered": report_covered, "research_total": report_total,
        "files": {
            "stock_pool": file_info(pool_path), "ranking": file_info(ranking_path),
            "classification": file_info(classification),
            "tags": file_info(tags), "zsxq": file_info(zsxq), "zsxq_audio": file_info(zsxq_audio),
            "research": file_info(research), "financial": file_info(financial),
        },
    }


def output_items() -> list[dict[str, Any]]:
    locations = [
        ("计算标的数量历史", project_path(config_value("files", "target_count_history", "data/output/计算标的数量历史.csv"))),
        ("分类数量历史", project_path(config_value("files", "classification_count_history", "data/output/分类数量历史.csv"))),
        ("低于200日线", OUTPUT_DIR / "沪深_低于200日线.csv"),
        ("20日均线附近", latest_optional("data/output/震荡上行_上升_赶顶_20日均线附近_*.csv")),
        ("200日均线附近", latest_optional("data/output/震荡上行_上升_赶顶_200日均线附近_*.csv")),
        ("股票页面", OUTPUT_DIR / "stock_pages" / "index.html"),
        ("市场日报", latest_optional("data/output/daily_briefs/*.html")),
        ("分类总表", latest_optional(str(config_value("files", "classification_pattern")))),
        ("机会评分", latest_optional("data/output/沪深_机会评分_*.csv")),
        ("机会评分回测", latest_optional("data/output/机会评分回测_排序质量_*.csv")),
        ("机会因子滚动验证", latest_optional("data/output/机会因子滚动汇总_*.csv")),
        ("历史覆盖审计", latest_optional("data/output/历史覆盖审计_*.csv")),
        ("产业标签", latest_tag_file()),
        ("知识星球语音", latest_zsxq_audio()),
        ("回测报告", latest_optional("data/output/分类历史回测_汇总_*.csv")),
    ]
    return [{"title": title, **file_info(path)} for title, path in locations if path and path.exists()]


RESULT_PREVIEW_COLUMNS = [
    "代码", "名称", "市场", "最新价", "涨跌幅", "5日涨跌幅", "20日涨跌幅",
    "60日涨跌幅", "250日涨跌幅", "所属行业", "市值",
    "市盈率TTM", "市净率", "换手率",
]
MARKET_CAP_DISPLAY_COLUMN = "市值(百亿)"


def prepare_stock_display(frame):
    """增加只用于网页展示的百亿市值列，不改动原始CSV。"""
    result = frame.copy()
    if "市值" in result.columns:
        values = pd.to_numeric(
            result["市值"].astype(str).str.replace(",", "", regex=False),
            errors="coerce",
        )
        result[MARKET_CAP_DISPLAY_COLUMN] = values.map(
            lambda value: "" if pd.isna(value) else f"{value / 10_000_000_000:.2f}"
        )
    return result


def display_columns(preferred: list[str], frame) -> list[str]:
    return [
        MARKET_CAP_DISPLAY_COLUMN if column == "市值" else column
        for column in preferred
        if (MARKET_CAP_DISPLAY_COLUMN if column == "市值" else column) in frame.columns
    ]


def stock_result_preview(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {"file": file_info(None), "columns": [], "all_column_count": 0, "rows": [], "total": 0}
    frame = prepare_stock_display(read_csv_auto(path, dtype=str).fillna(""))
    columns = display_columns(RESULT_PREVIEW_COLUMNS, frame)
    records = frame[columns].astype(str).to_dict(orient="records")
    return {
        "file": file_info(path), "columns": columns,
        "all_column_count": len(frame.columns), "rows": records, "total": len(frame),
    }


def below_ma200_preview() -> dict[str, Any]:
    return stock_result_preview(OUTPUT_DIR / "沪深_低于200日线.csv")


def nearby_ma_preview(period: int) -> dict[str, Any]:
    patterns = {
        20: "data/output/震荡上行_上升_赶顶_20日均线附近_*.csv",
        200: "data/output/震荡上行_上升_赶顶_200日均线附近_*.csv",
    }
    if period not in patterns:
        raise ValueError("均线周期只支持20或200")
    return stock_result_preview(latest_optional(patterns[period]))


def opportunity_score_preview() -> dict[str, Any]:
    path = latest_optional("data/output/沪深_机会评分_*.csv")
    if not path:
        return {"file": file_info(None), "columns": [], "rows": [], "total": 0}
    frame = prepare_stock_display(read_csv_auto(path, dtype=str).fillna(""))
    preferred = [
        "代码", "名称", "市场", "分类", "机会评分", "机会等级",
        "趋势贡献", "相对强弱贡献", "突破贡献", "确认贡献", "形态贡献",
        "风险扣分", "大盘调整", "信号覆盖率", "机会评分说明",
        "评分验证状态",
        "最新价", "涨跌幅", "5日涨跌幅", "20日涨跌幅", "60日涨跌幅",
        "所属行业", "市值",
    ]
    columns = display_columns(preferred, frame)
    return {
        "file": file_info(path), "columns": columns,
        "rows": frame[columns].astype(str).to_dict(orient="records"),
        "total": len(frame),
    }


def opportunity_factor_preview() -> dict[str, Any]:
    summary_path = latest_optional("data/output/机会因子滚动汇总_*.csv")
    monthly_path = latest_optional("data/output/机会因子滚动月度_*.csv")
    if not summary_path or not monthly_path:
        return {
            "summary_file": file_info(None), "monthly_file": file_info(None),
            "summary_rows": [], "monthly_rows": [], "summary_columns": [],
            "monthly_columns": [], "statistics": {},
        }
    summary = read_csv_auto(summary_path, dtype=str).fillna("")
    monthly = read_csv_auto(monthly_path, dtype=str).fillna("")
    for column in ["平均月度IC"]:
        if column in summary:
            summary[column] = pd.to_numeric(summary[column], errors="coerce").map(
                lambda value: f"{value:+.4f}" if pd.notna(value) else "—"
            )
    for column in ["IC为正月份比例", "Q5跑赢Q1月份比例"]:
        if column in summary:
            summary[column] = pd.to_numeric(summary[column], errors="coerce").map(
                lambda value: f"{value:.2%}" if pd.notna(value) else "—"
            )
    if "平均月度Q5减Q1" in summary:
        summary["平均月度Q5减Q1"] = pd.to_numeric(
            summary["平均月度Q5减Q1"], errors="coerce"
        ).map(lambda value: f"{value:+.2%}" if pd.notna(value) else "—")
    for column in ["趋势延续训练IC", "回撤反弹训练IC", "动态验证IC"]:
        if column in monthly:
            monthly[column] = pd.to_numeric(monthly[column], errors="coerce").map(
                lambda value: f"{value:+.4f}" if pd.notna(value) else "—"
            )
    if "动态验证Q5减Q1" in monthly:
        monthly["动态验证Q5减Q1"] = pd.to_numeric(
            monthly["动态验证Q5减Q1"], errors="coerce"
        ).map(lambda value: f"{value:+.2%}" if pd.notna(value) else "—")
    summary_columns = [column for column in [
        "周期", "验证方式", "覆盖月份数", "平均月度IC", "IC为正月份比例",
        "平均月度Q5减Q1", "Q5跑赢Q1月份比例", "结论",
    ] if column in summary]
    monthly_columns = [column for column in [
        "验证月份", "周期", "选择模型", "趋势延续训练IC", "回撤反弹训练IC",
        "动态验证IC", "动态验证Q5减Q1", "动态验证截面数", "选择原因", "信息截止",
    ] if column in monthly]
    passed = summary["结论"].eq("初步通过滚动验证") if "结论" in summary else pd.Series(dtype=bool)
    selections = monthly["选择模型"].value_counts().to_dict() if "选择模型" in monthly else {}
    return {
        "summary_file": file_info(summary_path), "monthly_file": file_info(monthly_path),
        "summary_columns": summary_columns,
        "summary_rows": summary[summary_columns].astype(str).to_dict(orient="records"),
        "monthly_columns": monthly_columns,
        "monthly_rows": monthly[monthly_columns].astype(str).to_dict(orient="records"),
        "statistics": {
            "passed_rows": int(passed.sum()), "total_rows": len(summary),
            "months": int(monthly["验证月份"].nunique()) if "验证月份" in monthly else 0,
            "rebound_selections": int(selections.get("回撤反弹", 0)),
            "trend_selections": int(selections.get("趋势延续", 0)),
            "abstentions": int(selections.get("不启用", 0)),
        },
        "warning": "研究候选：模型假设是在观察旧评分失败后提出，且使用当前股票池，不能视为独立样本实盘结论。",
    }


def history_coverage_preview() -> dict[str, Any]:
    path = latest_optional("data/output/历史覆盖审计_*.csv")
    if not path:
        return {"file": file_info(None), "columns": [], "rows": [], "total": 0, "summary": {}}
    frame = read_csv_auto(path, dtype=str).fillna("")
    preferred = [
        "代码", "名称", "证券类型", "历史状态", "起始日期", "结束日期", "交易日数",
        "起始覆盖", "距最新基准交易日", "相对基准缺口天数(含停牌)",
        "相对基准缺口率", "5日非重叠截面", "20日非重叠截面",
        "60日非重叠截面", "校验状态",
    ]
    columns = [column for column in preferred if column in frame.columns]
    stocks = frame[frame.get("证券类型", "股票").eq("股票")] if "证券类型" in frame else frame
    counts = stocks["历史状态"].value_counts().to_dict() if "历史状态" in stocks else {}
    trading_days = pd.to_numeric(stocks["交易日数"], errors="coerce") if "交易日数" in stocks else pd.Series(dtype=float)
    sixty = pd.to_numeric(stocks["60日非重叠截面"], errors="coerce") if "60日非重叠截面" in stocks else pd.Series(dtype=float)
    return {
        "file": file_info(path), "columns": columns,
        "rows": frame[columns].astype(str).to_dict(orient="records"), "total": len(frame),
        "summary": {
            "complete": int(counts.get("完整", 0)),
            "usable": int(counts.get("可回测但不完整", 0)),
            "insufficient": int(counts.get("不足", 0) + counts.get("缺失", 0)),
            "median_trading_days": int(trading_days.median()) if trading_days.notna().any() else 0,
            "long_ready": int((sixty >= 10).sum()) if sixty.notna().any() else 0,
            "unsupported_indexes": int((frame.get("历史状态") == "数据源不支持").sum()) if "历史状态" in frame else 0,
        },
    }


def target_count_history_preview() -> dict[str, Any]:
    path = project_path(config_value("files", "target_count_history", "data/output/计算标的数量历史.csv"))
    columns = ["日期", "强势数量", "近期新高数量", "历史新高数量"]
    if not path.exists():
        return {"file": file_info(None), "columns": columns, "rows": [], "total": 0, "latest": {}}
    frame = read_csv_auto(path, dtype={"日期": str}).fillna("").reindex(columns=columns)
    frame = frame.sort_values("日期", ascending=False)
    rows = frame.astype(str).to_dict(orient="records")
    return {
        "file": file_info(path), "columns": columns, "rows": rows,
        "total": len(rows), "latest": rows[0] if rows else {},
    }


def classification_count_history_preview() -> dict[str, Any]:
    path = project_path(config_value("files", "classification_count_history", "data/output/分类数量历史.csv"))
    columns = ["日期", *[f"{name}数量" for name in CLASSIFICATION_NAMES]]
    if not path.exists():
        return {"file": file_info(None), "columns": columns, "rows": [], "total": 0, "latest": {}}
    frame = read_csv_auto(path, dtype={"日期": str}).fillna("").reindex(columns=columns)
    frame = frame.sort_values("日期", ascending=False)
    rows = frame.astype(str).to_dict(orient="records")
    return {
        "file": file_info(path), "columns": columns, "rows": rows,
        "total": len(rows), "latest": rows[0] if rows else {},
    }


def _json_safe_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    clean = frame.astype(object).where(pd.notna(frame), None)
    return clean.to_dict(orient="records")


def _comparison_output_path(metadata: dict[str, Any], label: str) -> Path | None:
    raw = metadata.get("outputs", {}).get(label)
    if not raw:
        return None
    path = Path(raw).resolve()
    output_root = OUTPUT_DIR.resolve()
    if path != output_root and output_root not in path.parents:
        return None
    return path if path.is_file() else None


def rule_comparison_preview() -> dict[str, Any]:
    """读取同一次规则实验的元数据和结果，避免混用不同时间戳文件。"""
    metadata_path = latest_optional("data/output/分类规则对比_元数据_*.json")
    if not metadata_path:
        return {"available": False, "metadata_file": file_info(None)}
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"规则对比元数据无法读取：{exc}") from exc

    frames: dict[str, pd.DataFrame] = {}
    for label in (
        "表现", "稳定性", "基线差异", "覆盖率",
        "变化样本", "迁移代表股票", "阈值变化",
    ):
        path = _comparison_output_path(metadata, label)
        frames[label] = read_csv_auto(path) if path else pd.DataFrame()

    performance = frames["表现"]
    deltas = frames["基线差异"]
    rules = [
        {
            "name": name,
            "description": values.get("description", name),
            "sha256": values.get("sha256", ""),
        }
        for name, values in metadata.get("rules", {}).items()
    ]
    outputs = {
        label: file_info(_comparison_output_path(metadata, label))
        for label in metadata.get("outputs", {})
    }
    return {
        "available": True,
        "metadata_file": file_info(metadata_path),
        "created_at": metadata.get("created_at", ""),
        "snapshot_dates": metadata.get("snapshot_dates", []),
        "snapshot_count": len(metadata.get("snapshot_dates", [])),
        "horizons": metadata.get("horizons", []),
        "periods": performance["样本区间"].dropna().drop_duplicates().tolist()
        if "样本区间" in performance else [],
        "step": metadata.get("step"),
        "statistics": metadata.get("statistics", {}),
        "rules": rules,
        "performance": _json_safe_records(performance),
        "stability": _json_safe_records(frames["稳定性"]),
        "deltas": _json_safe_records(deltas),
        "coverage": _json_safe_records(frames["覆盖率"]),
        "migrations": _json_safe_records(frames["变化样本"]),
        "migration_stocks": _json_safe_records(frames["迁移代表股票"]),
        "threshold_changes": _json_safe_records(frames["阈值变化"]),
        "outputs": outputs,
    }


def stock_list_preview(source: str, category: str, date: str) -> dict[str, Any]:
    date_tag = str(date).replace("-", "")
    if not re.fullmatch(r"20\d{6}", date_tag):
        raise ValueError("日期必须为YYYY-MM-DD或YYYYMMDD")
    if source == "target":
        if category not in TARGET_NAMES:
            raise ValueError("未知计算标的类型")
        path = OUTPUT_DIR / "计算标的" / f"{category}_{date_tag}.csv"
    elif source == "classification":
        if category not in CLASSIFICATION_NAMES:
            raise ValueError("未知分类类型")
        path = OUTPUT_DIR / f"沪深_{category}_{date_tag}.csv"
    else:
        raise ValueError("未知股票列表来源")
    preferred = [
        "代码", "名称", "市场", "最新价", "涨跌幅", "5日涨跌幅", "20日涨跌幅",
        "60日涨跌幅", "所属行业", "市值", "市盈率TTM", "换手率",
    ]
    if not path.exists():
        return {
            "source": source, "category": category, "date": date_tag,
            "file": file_info(None), "columns": [], "rows": [], "total": 0,
        }
    frame = prepare_stock_display(read_csv_auto(path, dtype=str).fillna(""))
    columns = display_columns(preferred, frame)
    return {
        "source": source, "category": category, "date": date_tag,
        "file": file_info(path), "columns": columns,
        "rows": frame[columns].astype(str).to_dict(orient="records"),
        "total": len(frame),
    }


manager = JobManager()
app = FastAPI(title="A股研究控制台", version="0.1.0")


@app.middleware("http")
async def disable_ui_cache(request, call_next):
    response = await call_next(request)
    if request.url.path in {
        "/", "/index.html", "/app.js", "/styles.css", "/migration.js",
        "/migration.css", "/opportunity.js", "/factor-validation.js", "/history.js"
    }:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/api/status")
def api_status():
    return dashboard_status()


@app.get("/api/tasks")
def api_tasks():
    return [
        {"id": key, "title": task.title, "description": task.description,
         "category": task.category, "network": task.network, "options": list(task.allowed)}
        for key, task in TASKS.items()
    ]


@app.post("/api/tasks/{task_id}")
def api_start_task(task_id: str, request: TaskRequest):
    if task_id not in TASKS:
        raise HTTPException(404, "未知任务")
    try:
        return manager.start(task_id, request.options)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/jobs")
def api_jobs():
    return manager.list()


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str):
    try:
        return manager.cancel(job_id)
    except KeyError as exc:
        raise HTTPException(404, "任务不存在") from exc


@app.get("/api/outputs")
def api_outputs():
    return output_items()


@app.get("/api/previews/below-ma200")
def api_below_ma200_preview():
    return below_ma200_preview()


@app.get("/api/previews/nearby-ma/{period}")
def api_nearby_ma_preview(period: int):
    try:
        return nearby_ma_preview(period)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/previews/opportunity-scores")
def api_opportunity_score_preview():
    return opportunity_score_preview()


@app.get("/api/previews/opportunity-factors")
def api_opportunity_factor_preview():
    return opportunity_factor_preview()


@app.get("/api/previews/history-coverage")
def api_history_coverage_preview():
    return history_coverage_preview()


@app.get("/api/previews/target-count-history")
def api_target_count_history_preview():
    return target_count_history_preview()


@app.get("/api/previews/classification-count-history")
def api_classification_count_history_preview():
    return classification_count_history_preview()


@app.get("/api/previews/rule-comparison")
def api_rule_comparison_preview():
    try:
        return rule_comparison_preview()
    except ValueError as exc:
        raise HTTPException(500, str(exc)) from exc


@app.get("/api/previews/stocks")
def api_stock_list_preview(source: str, category: str, date: str):
    try:
        return stock_list_preview(source, category, date)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/output/{relative_path:path}")
def open_output(relative_path: str):
    target = (PROJECT_DIR / relative_path).resolve()
    if OUTPUT_DIR.resolve() not in target.parents and target != OUTPUT_DIR.resolve():
        raise HTTPException(403, "只允许访问输出目录")
    if not target.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(target)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")


def main() -> None:
    import uvicorn
    uvicorn.run("web_console:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
