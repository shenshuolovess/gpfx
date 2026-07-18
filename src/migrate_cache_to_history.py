"""将现有 Baostock 缓存合并进不可清理的正式历史库。"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path

import pandas as pd

from history_store import merge_history
from pipeline_config import config_value, project_path
from stock_utils import read_csv_auto


_CACHE_RE = re.compile(r"^(sh|sz|bj)_(\d{6})_.*_adj(\d+)\.csv$", re.IGNORECASE)


def discover_cache_groups(cache_dir: Path) -> dict[str, list[Path]]:
    groups: dict[str, list[Path]] = defaultdict(list)
    for path in cache_dir.glob("*.csv"):
        match = _CACHE_RE.match(path.name)
        if match:
            groups[f"{match.group(1).lower()}.{match.group(2)}"].append(path)
    return dict(groups)


def migrate(cache_dir: Path, history_dir: Path, *, benchmark_code: str) -> tuple[int, int, int]:
    groups = discover_cache_groups(cache_dir)
    migrated = skipped = failed = 0
    for number, (code, paths) in enumerate(sorted(groups.items()), start=1):
        try:
            frames = [read_csv_auto(path) for path in sorted(paths)]
            combined = pd.concat(frames, ignore_index=True, sort=False)
            if combined.empty or not {"date", "close"}.issubset(combined.columns):
                skipped += 1
                print(f"[跳过空缓存] {code}")
                continue
            kind = "benchmark" if code == benchmark_code else "daily"
            merge_history(
                history_dir,
                code,
                combined,
                kind=kind,
                source="baostock-cache-migration",
                adjustflag="3" if kind == "benchmark" else "2",
            )
            migrated += 1
        except Exception as exc:
            failed += 1
            print(f"[迁移失败] {code} | {exc}")
        if number % 50 == 0 or number == len(groups):
            print(
                f"[迁移进度] {number}/{len(groups)} | "
                f"成功 {migrated} | 跳过 {skipped} | 失败 {failed}"
            )
    return migrated, skipped, failed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="把行情缓存一次性迁移到正式历史库")
    parser.add_argument(
        "--cache-dir",
        default=config_value("files", "rating_cache_dir", "cache/baostock"),
    )
    parser.add_argument(
        "--history-dir",
        default=config_value("files", "history_dir", "data/history"),
    )
    parser.add_argument("--benchmark-code", default="sh.000300")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    cache_dir = project_path(args.cache_dir)
    history_dir = project_path(args.history_dir)
    if not cache_dir.is_dir():
        raise FileNotFoundError(f"缓存目录不存在：{cache_dir}")
    migrated, skipped, failed = migrate(
        cache_dir, history_dir, benchmark_code=args.benchmark_code
    )
    print(
        f"迁移完成：成功 {migrated}，跳过空缓存 {skipped}，"
        f"失败 {failed}，历史库 {history_dir}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
