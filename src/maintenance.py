"""轮转日志与缓存清理命令；默认预览，--apply 后才实际删除。"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from pipeline_config import config_value, project_path


_BAOSTOCK_NAME_RE = re.compile(r"^(sh|sz|bj)_(\d{6})_.*_adj\d+\.csv$", re.IGNORECASE)
_CACHE_END_DATE_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})_adj\d+\.csv$")


@dataclass(frozen=True)
class CleanupCandidate:
    path: Path
    reason: str
    size: int


def _older_than(path: Path, cutoff: datetime) -> bool:
    return datetime.fromtimestamp(path.stat().st_mtime) < cutoff


def find_baostock_candidates(
    cache_dir: Path,
    *,
    keep_per_code: int,
    cutoff: datetime,
) -> list[CleanupCandidate]:
    groups: dict[str, list[Path]] = defaultdict(list)
    for path in cache_dir.glob("*.csv"):
        match = _BAOSTOCK_NAME_RE.match(path.name)
        if match:
            groups[f"{match.group(1).lower()}_{match.group(2)}"].append(path)

    candidates: list[CleanupCandidate] = []
    for files in groups.values():
        def sort_key(path: Path):
            match = _CACHE_END_DATE_RE.search(path.name)
            return match.group(1) if match else "0000-00-00", path.stat().st_mtime_ns

        files.sort(key=sort_key, reverse=True)
        for path in files[max(0, keep_per_code):]:
            if _older_than(path, cutoff):
                candidates.append(
                    CleanupCandidate(path, f"超过每代码保留 {keep_per_code} 份且已过期", path.stat().st_size)
                )
    return candidates


def find_generic_cache_candidates(
    cache_root: Path,
    *,
    baostock_dir: Path,
    cutoff: datetime,
    protected_roots: tuple[Path, ...] = (),
) -> list[CleanupCandidate]:
    candidates = []
    protected = tuple(root.resolve() for root in protected_roots)
    for path in cache_root.rglob("*"):
        if not path.is_file() or baostock_dir in path.parents:
            continue
        resolved = path.resolve()
        if any(resolved == root or root in resolved.parents for root in protected):
            continue
        if _older_than(path, cutoff):
            candidates.append(CleanupCandidate(path, "过期运行缓存", path.stat().st_size))
    return candidates


def find_log_candidates(roots: list[Path], *, cutoff: datetime) -> list[CleanupCandidate]:
    candidates = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.log*"):
            resolved = path.resolve()
            if resolved in seen or not path.is_file():
                continue
            seen.add(resolved)
            if _older_than(path, cutoff):
                candidates.append(CleanupCandidate(path, "过期日志", path.stat().st_size))
    return candidates


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="预览或清理过期缓存和日志",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--apply", action="store_true", help="实际删除；不加时仅预览")
    parser.add_argument("--cache-root", default="cache", help="缓存根目录")
    parser.add_argument(
        "--baostock-dir",
        default=config_value("files", "rating_cache_dir", "cache/baostock"),
        help="Baostock 缓存目录",
    )
    parser.add_argument(
        "--output-dir",
        default=config_value("files", "output_dir", "data/output"),
        help="输出与日志目录",
    )
    parser.add_argument(
        "--history-dir",
        default=config_value("files", "history_dir", "data/history"),
        help="受保护的正式历史库，永不作为缓存清理",
    )
    parser.add_argument(
        "--cache-days",
        type=int,
        default=int(config_value("maintenance", "cache_retention_days", 30)),
        help="缓存最短保留天数",
    )
    parser.add_argument(
        "--keep-per-code",
        type=int,
        default=int(config_value("maintenance", "cache_keep_per_code", 3)),
        help="每个股票代码至少保留的行情缓存份数",
    )
    parser.add_argument(
        "--log-days",
        type=int,
        default=int(config_value("maintenance", "log_retention_days", 30)),
        help="日志保留天数",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.cache_days < 0 or args.log_days < 0 or args.keep_per_code < 1:
        raise ValueError("保留天数不能为负数，keep-per-code 必须至少为 1")

    now = datetime.now()
    cache_root = project_path(args.cache_root)
    baostock_dir = project_path(args.baostock_dir)
    output_dir = project_path(args.output_dir)
    history_dir = project_path(args.history_dir)
    cache_cutoff = now - timedelta(days=args.cache_days)
    log_cutoff = now - timedelta(days=args.log_days)

    candidates = []
    if baostock_dir.exists():
        candidates.extend(
            find_baostock_candidates(
                baostock_dir,
                keep_per_code=args.keep_per_code,
                cutoff=cache_cutoff,
            )
        )
    if cache_root.exists():
        candidates.extend(
            find_generic_cache_candidates(
                cache_root,
                baostock_dir=baostock_dir,
                cutoff=cache_cutoff,
                protected_roots=(history_dir,),
            )
        )
    candidates.extend(find_log_candidates([output_dir, cache_root], cutoff=log_cutoff))

    # 同一日志可能同时属于通用缓存和日志，只处理一次。
    unique = {candidate.path.resolve(): candidate for candidate in candidates}
    candidates = sorted(unique.values(), key=lambda item: str(item.path))
    total_size = sum(item.size for item in candidates)

    mode = "执行删除" if args.apply else "仅预览"
    print(f"模式：{mode}")
    print(f"候选文件：{len(candidates)}")
    print(f"可释放空间：{total_size / 1024 / 1024:.2f} MB")
    for candidate in candidates[:30]:
        print(f"- {candidate.path} | {candidate.reason} | {candidate.size / 1024:.1f} KB")
    if len(candidates) > 30:
        print(f"... 其余 {len(candidates) - 30} 个文件省略")

    if not args.apply:
        print("未删除任何文件；确认后加 --apply 执行。")
        return 0

    deleted = 0
    failed = 0
    for candidate in candidates:
        try:
            candidate.path.unlink()
            deleted += 1
        except OSError as exc:
            failed += 1
            print(f"删除失败：{candidate.path} | {exc}")
    print(f"删除完成：成功 {deleted}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
