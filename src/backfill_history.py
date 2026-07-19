"""从 Baostock 补全正式历史库；串行限速、原子合并、支持断点续跑。"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

import baostock as bs
import pandas as pd

from history_coverage import BENCHMARK_CODE, audit_history_coverage, write_coverage_audit
from history_store import merge_history
from pipeline_config import config_value, project_path, resolve_input
from stock_utils import normalize_code, read_csv_auto


FIELDS = "date,open,high,low,close,volume,amount"
INDEX_CODES = {
    "sh.000001", "sh.000016", "sh.000300", "sh.000688", "sh.000905",
    "sz.399001", "sz.399006",
}
UNSUPPORTED_BAOSTOCK_CODES = {"sh.000688"}


def _atomic_json(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "completed": {}, "failures": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "completed": {}, "failures": {}}


def query_daily(code: str, start: str, end: str, adjustflag: str, retries: int = 3) -> pd.DataFrame:
    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            result = bs.query_history_k_data_plus(
                code, FIELDS, start_date=start, end_date=end,
                frequency="d", adjustflag=adjustflag,
            )
            if result.error_code != "0":
                raise RuntimeError(result.error_msg)
            rows = []
            while result.next():
                rows.append(result.get_row_data())
            return pd.DataFrame(rows, columns=result.fields or FIELDS.split(","))
        except Exception as exc:
            last_error = str(exc)
            if attempt < retries:
                time.sleep(attempt * 2)
                bs.login()
    raise RuntimeError(last_error or "未知网络错误")


def request_key(code: str, start: str, end: str, adjustflag: str) -> str:
    return f"{normalize_code(code, 'baostock')}|{start}|{end}|adj{adjustflag}"


def backfill(
    pool: pd.DataFrame, history_dir: Path, *, start: str, end: str,
    interval: float, state_path: Path, force: bool = False,
) -> tuple[int, int, int]:
    state = load_state(state_path)
    securities = [(BENCHMARK_CODE, "benchmark", "3", "沪深300基准")]
    seen = {normalize_code(BENCHMARK_CODE, "baostock")}
    for _, row in pool.iterrows():
        code = normalize_code(row.get("代码", ""), "baostock")
        if not code or code in seen:
            continue
        seen.add(code)
        adjustflag = "3" if code in INDEX_CODES else "2"
        securities.append((code, "daily", adjustflag, str(row.get("名称", ""))))
    succeeded = skipped = failed = 0
    total = len(securities)
    for index, (code, kind, adjustflag, name) in enumerate(securities, start=1):
        key = request_key(code, start, end, adjustflag)
        if code in UNSUPPORTED_BAOSTOCK_CODES:
            state.setdefault("completed", {})[key] = {
                "code": code, "status": "source_unsupported",
                "requested_start": start, "requested_end": end,
                "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            state.setdefault("failures", {}).pop(code, None)
            skipped += 1
            _atomic_json(state, state_path)
            print(f"[{index}/{total}] 数据源不支持，已跳过：{code} {name}", flush=True)
            continue
        if not force and key in state.get("completed", {}):
            skipped += 1
            print(f"[{index}/{total}] 已有断点：{code} {name}", flush=True)
            continue
        started = time.time()
        try:
            frame = query_daily(code, start, end, adjustflag)
            if frame.empty:
                raise RuntimeError("返回空行情")
            merge_history(
                history_dir, code, frame, kind=kind, source="baostock-backfill",
                adjustflag=adjustflag,
            )
            state.setdefault("completed", {})[key] = {
                "code": code, "requested_start": start, "requested_end": end,
                "rows_received": len(frame),
                "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            state.setdefault("failures", {}).pop(code, None)
            succeeded += 1
            print(
                f"[{index}/{total}] 补全成功：{code} {name} | {len(frame)}行 | "
                f"{time.time()-started:.1f}秒", flush=True,
            )
        except Exception as exc:
            failed += 1
            state.setdefault("failures", {})[code] = {
                "error": str(exc),
                "failed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            print(f"[{index}/{total}] 补全失败：{code} {name} | {exc}", flush=True)
        state["last_request"] = {"start": start, "end": end, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        _atomic_json(state, state_path)
        if interval > 0 and index < total:
            time.sleep(interval)
    return succeeded, skipped, failed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="从2021年起补全正式历史行情")
    parser.add_argument("--pool")
    parser.add_argument("--history-dir", default=config_value("files", "history_dir", "data/history"))
    parser.add_argument("--output-dir", default=config_value("files", "output_dir", "data/output"))
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--interval", type=float, default=.35)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if pd.Timestamp(args.start) > pd.Timestamp(args.end):
        raise ValueError("start不能晚于end")
    if args.interval < 0:
        raise ValueError("interval不能为负数")
    pool_file = resolve_input(args.pool, config_key="stock_pool")
    pool = read_csv_auto(pool_file, dtype=str)
    history_dir, output_dir = project_path(args.history_dir), project_path(args.output_dir)
    state_path = history_dir / "backfill_state.json"
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"baostock登录失败：{login.error_msg}")
    try:
        succeeded, skipped, failed = backfill(
            pool, history_dir, start=args.start, end=args.end,
            interval=args.interval, state_path=state_path, force=args.force,
        )
    finally:
        try:
            bs.logout()
        except Exception:
            pass
    audit = audit_history_coverage(pool, history_dir, target_start=args.start)
    audit_path = write_coverage_audit(audit, output_dir)
    print(f"补全结束：成功{succeeded}，断点跳过{skipped}，失败{failed}")
    print(f"断点文件：{state_path}")
    print(f"覆盖审计：{audit_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
