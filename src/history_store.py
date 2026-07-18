"""可复现回测使用的持久行情库。

`data/history` 是正式数据，不是缓存。每个证券只保留一份按日期合并的 CSV，
并用 manifest.json 记录覆盖范围和校验和。所有写入均先写临时文件再原子替换。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal

import pandas as pd

from stock_utils import normalize_code, read_csv_auto


HistoryKind = Literal["daily", "benchmark"]
REQUIRED_COLUMNS = ("date", "close")
NUMERIC_COLUMNS = ("open", "high", "low", "close", "volume", "amount")


def history_file(history_dir: str | Path, code: str, *, kind: HistoryKind = "daily") -> Path:
    bs_code = normalize_code(code, "baostock")
    if not bs_code:
        raise ValueError(f"无效证券代码：{code!r}")
    market, digits = bs_code.split(".", 1)
    return Path(history_dir) / kind / market / f"{digits}.csv"


def _clean_frame(frame: pd.DataFrame) -> pd.DataFrame:
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"行情缺少必要列：{missing}")

    cleaned = frame.copy()
    cleaned.columns = [str(column).strip() for column in cleaned.columns]
    parsed_dates = pd.to_datetime(cleaned["date"], errors="coerce")
    cleaned = cleaned.loc[parsed_dates.notna()].copy()
    cleaned["date"] = parsed_dates[parsed_dates.notna()].dt.strftime("%Y-%m-%d")
    cleaned = cleaned.dropna(subset=["close"])
    if cleaned.empty:
        raise ValueError("行情中没有有效的 date/close 记录")

    for column in NUMERIC_COLUMNS:
        if column in cleaned.columns:
            numeric = pd.to_numeric(cleaned[column], errors="coerce")
            cleaned[column] = numeric

    cleaned = cleaned.dropna(subset=["close"])
    cleaned = cleaned.sort_values("date").drop_duplicates("date", keep="last")
    return cleaned.reset_index(drop=True)


def _atomic_write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        frame.to_csv(temp_path, index=False, encoding="utf-8-sig")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _atomic_write_json(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _update_manifest(
    history_dir: Path,
    path: Path,
    frame: pd.DataFrame,
    *,
    code: str,
    kind: HistoryKind,
    source: str,
    adjustflag: str,
) -> None:
    manifest_path = history_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
    else:
        manifest = {}
    manifest.setdefault("schema_version", 1)
    manifest.setdefault("securities", {})
    key = f"{kind}:{normalize_code(code, 'baostock')}"
    manifest["securities"][key] = {
        "code": normalize_code(code, "baostock"),
        "kind": kind,
        "source": source,
        "adjustflag": str(adjustflag),
        "path": path.relative_to(history_dir).as_posix(),
        "min_date": str(frame["date"].iloc[0]),
        "max_date": str(frame["date"].iloc[-1]),
        "rows": int(len(frame)),
        "sha256": file_sha256(path),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _atomic_write_json(manifest, manifest_path)


def merge_history(
    history_dir: str | Path,
    code: str,
    incoming: pd.DataFrame,
    *,
    kind: HistoryKind = "daily",
    source: str = "baostock",
    adjustflag: str = "2",
) -> Path:
    """把新行情合并到正式历史库；同一日期以本次传入数据为准。"""
    root = Path(history_dir)
    path = history_file(root, code, kind=kind)
    frames = []
    if path.exists():
        frames.append(read_csv_auto(path))
    frames.append(incoming)
    merged = _clean_frame(pd.concat(frames, ignore_index=True, sort=False))
    _atomic_write_csv(merged, path)
    _update_manifest(
        root,
        path,
        merged,
        code=code,
        kind=kind,
        source=source,
        adjustflag=adjustflag,
    )
    return path


def load_history(
    history_dir: str | Path,
    code: str,
    *,
    kind: HistoryKind = "daily",
    verify_checksum: bool = False,
) -> pd.DataFrame | None:
    path = history_file(history_dir, code, kind=kind)
    if not path.is_file():
        return None
    if verify_checksum:
        manifest_path = Path(history_dir) / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        key = f"{kind}:{normalize_code(code, 'baostock')}"
        expected = manifest.get("securities", {}).get(key, {}).get("sha256")
        if not expected or file_sha256(path) != expected:
            raise RuntimeError(f"历史行情校验失败：{path}")
    return _clean_frame(read_csv_auto(path))


def archive_run_snapshot(
    history_dir: str | Path,
    date_tag: str,
    *,
    pool_file: str | Path,
    signals: pd.DataFrame,
    rules_file: str | Path,
) -> Path:
    """保存当日股票池、实际分类结果和分类规则版本。"""
    root = Path(history_dir)
    snapshot_dir = root / "snapshots"
    pool_target = snapshot_dir / "pools" / f"{date_tag}.csv"
    signal_target = snapshot_dir / "signals" / f"{date_tag}.csv"
    pool_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pool_file, pool_target)
    _atomic_write_csv(signals, signal_target)

    rules_hash = file_sha256(rules_file)
    rules_target = snapshot_dir / "rules" / f"classification_rules_{rules_hash[:12]}.py"
    rules_target.parent.mkdir(parents=True, exist_ok=True)
    if not rules_target.exists():
        shutil.copy2(rules_file, rules_target)

    run_record = {
        "date": date_tag,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pool": pool_target.relative_to(root).as_posix(),
        "signals": signal_target.relative_to(root).as_posix(),
        "rules": rules_target.relative_to(root).as_posix(),
        "rules_sha256": rules_hash,
    }
    run_path = snapshot_dir / "runs" / f"{date_tag}.json"
    _atomic_write_json(run_record, run_path)
    return run_path
