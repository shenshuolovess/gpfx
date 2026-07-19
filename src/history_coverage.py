"""审计正式历史库覆盖范围、时效、缺口和回测可用截面。"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd

from history_store import file_sha256, history_file, load_history
from pipeline_config import config_value, project_path, resolve_input
from stock_utils import normalize_code, read_csv_auto, timestamped_output_path, write_csv


MIN_SIGNAL_ROWS = 220
BENCHMARK_CODE = "sh.000300"
INDEX_CODES = {
    "000001.SH", "000016.SH", "000300.SH", "000688.SH", "000905.SH",
    "399001.SZ", "399006.SZ",
}
UNSUPPORTED_SOURCE_CODES = {"000688.SH"}


def non_overlapping_snapshot_count(rows: int, horizon: int) -> int:
    usable = rows - MIN_SIGNAL_ROWS - horizon + 1
    return 0 if usable <= 0 else 1 + (usable - 1) // horizon


def audit_history_coverage(
    pool: pd.DataFrame,
    history_dir: Path,
    *,
    target_start: str,
    benchmark_code: str = BENCHMARK_CODE,
) -> pd.DataFrame:
    benchmark = load_history(history_dir, benchmark_code, kind="benchmark")
    if benchmark is None or benchmark.empty:
        raise FileNotFoundError(f"正式历史库缺少基准：{benchmark_code}")
    benchmark_dates = pd.to_datetime(benchmark["date"], errors="coerce").dropna().sort_values()
    latest_benchmark = benchmark_dates.iloc[-1]
    target_candidates = benchmark_dates[benchmark_dates >= pd.Timestamp(target_start)]
    target_trading_start = target_candidates.iloc[0] if len(target_candidates) else pd.Timestamp(target_start)
    rows = []
    for _, item in pool.iterrows():
        raw_code = item.get("代码", "")
        code = normalize_code(raw_code, "suffix")
        security_type = "指数" if code in INDEX_CODES else "股票"
        bs_code = normalize_code(raw_code, "baostock")
        kind = "benchmark" if bs_code == normalize_code(benchmark_code, "baostock") else "daily"
        path = history_file(history_dir, raw_code, kind=kind)
        history = load_history(history_dir, raw_code, kind=kind) if path.exists() else None
        if history is None or history.empty:
            missing_status = "数据源不支持" if code in UNSUPPORTED_SOURCE_CODES else "缺失"
            rows.append({
                "代码": code, "名称": str(item.get("名称", "")), "证券类型": security_type,
                "历史状态": missing_status,
                "起始日期": "", "结束日期": "", "交易日数": 0,
                "目标起始日期": target_start, "起始覆盖": "未覆盖",
                "距最新基准交易日": "", "相对基准缺口天数(含停牌)": "",
                "相对基准缺口率": "", "5日非重叠截面": 0,
                "20日非重叠截面": 0, "60日非重叠截面": 0,
                "校验状态": "无文件", "历史文件": "",
            })
            continue
        dates = pd.to_datetime(history["date"], errors="coerce").dropna().sort_values()
        first, last = dates.iloc[0], dates.iloc[-1]
        expected = benchmark_dates[(benchmark_dates >= first) & (benchmark_dates <= last)]
        actual = set(dates.dt.strftime("%Y-%m-%d"))
        missing_days = sum(date.strftime("%Y-%m-%d") not in actual for date in expected)
        gap_rate = missing_days / len(expected) if len(expected) else 0.0
        latest_lag = int((benchmark_dates > last).sum())
        start_ok = first <= target_trading_start
        row_count = len(dates)
        checksum = "未登记"
        manifest_path = history_dir / "manifest.json"
        if manifest_path.exists():
            try:
                import json
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                key = f"{kind}:{bs_code}"
                expected_hash = manifest.get("securities", {}).get(key, {}).get("sha256")
                checksum = "通过" if expected_hash and file_sha256(path) == expected_hash else "失败"
            except Exception:
                checksum = "失败"
        if start_ok and latest_lag <= 3 and gap_rate <= .05 and checksum == "通过":
            status = "完整"
        elif row_count >= MIN_SIGNAL_ROWS + 60 and latest_lag <= 10 and checksum == "通过":
            status = "可回测但不完整"
        else:
            status = "不足"
        rows.append({
            "代码": code, "名称": str(item.get("名称", "")), "证券类型": security_type,
            "历史状态": status,
            "起始日期": first.strftime("%Y-%m-%d"), "结束日期": last.strftime("%Y-%m-%d"),
            "交易日数": row_count, "目标起始日期": target_start,
            "起始覆盖": "达标" if start_ok else "晚于目标（新股或缺历史）",
            "距最新基准交易日": latest_lag,
            "相对基准缺口天数(含停牌)": missing_days,
            "相对基准缺口率": round(gap_rate, 4),
            "5日非重叠截面": non_overlapping_snapshot_count(row_count, 5),
            "20日非重叠截面": non_overlapping_snapshot_count(row_count, 20),
            "60日非重叠截面": non_overlapping_snapshot_count(row_count, 60),
            "校验状态": checksum,
            "历史文件": str(path),
        })
    return pd.DataFrame(rows)


def write_coverage_audit(
    frame: pd.DataFrame, output_dir: Path, *, timestamp: str | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = timestamped_output_path(
        output_dir, "历史覆盖审计",
        timestamp=timestamp or datetime.now().strftime("%Y%m%d_%H%M%S"), suffix=".csv",
    )
    write_csv(frame, path)
    return path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="审计正式历史库覆盖质量")
    parser.add_argument("--pool")
    parser.add_argument("--history-dir", default=config_value("files", "history_dir", "data/history"))
    parser.add_argument("--output-dir", default=config_value("files", "output_dir", "data/output"))
    parser.add_argument("--target-start", default="2021-01-01")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    pool_file = resolve_input(args.pool, config_key="stock_pool")
    pool = read_csv_auto(pool_file, dtype=str)
    audit = audit_history_coverage(
        pool, project_path(args.history_dir), target_start=args.target_start,
    )
    path = write_coverage_audit(audit, project_path(args.output_dir))
    print(f"审计股票：{len(audit)}")
    print(audit["历史状态"].value_counts(dropna=False).to_string())
    print(f"审计文件：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
