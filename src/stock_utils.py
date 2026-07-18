"""股票脚本共享的无状态工具函数。"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Literal, Sequence

import pandas as pd


CSV_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "utf-8", "gb18030", "gbk")
_DATE_RE = re.compile(r"(?<!\d)(20\d{6})(?!\d)")
_SIX_DIGIT_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")

CodeStyle = Literal["digits", "suffix", "baostock"]


def previous_trading_day(reference: date | datetime | None = None) -> date:
    """返回严格早于参考日期的最近工作日（周一至周五）。"""
    current = reference.date() if isinstance(reference, datetime) else (reference or date.today())
    candidate = current - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _code_digits(value) -> str:
    if value is None or pd.isna(value):
        return ""

    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        if value.is_integer():
            value = int(value)

    text = str(value).strip().upper()
    if not text:
        return ""

    match = _SIX_DIGIT_RE.search(text)
    if match:
        return match.group(1)

    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return ""
    return digits[-6:].zfill(6)


def market_suffix(code6: str) -> str:
    """根据六位代码返回 SH、SZ 或 BJ。"""
    if code6.startswith(("4", "8")) or code6.startswith("92"):
        return "BJ"
    if code6.startswith(("5", "6", "9")):
        return "SH"
    return "SZ"


def _explicit_market(value) -> str | None:
    text = str(value).strip().upper()
    if text.startswith("SH") or text.endswith((".SH", ".XSHG")):
        return "SH"
    if text.startswith("SZ") or text.endswith((".SZ", ".XSHE")):
        return "SZ"
    if text.startswith("BJ") or text.endswith(".BJ"):
        return "BJ"
    return None


def normalize_code(value, style: CodeStyle = "digits") -> str:
    """将常见股票代码格式统一为六位、后缀式或 Baostock 式。"""
    code6 = _code_digits(value)
    if not code6:
        return ""
    market = _explicit_market(value) or market_suffix(code6)
    if style == "digits":
        return code6
    if style == "suffix":
        return f"{code6}.{market}"
    if style == "baostock":
        return f"{market.lower()}.{code6}"
    raise ValueError(f"不支持的代码格式：{style}")


def normalize_code_digits(value) -> str:
    return normalize_code(value, "digits")


def normalize_code_suffix(value) -> str:
    return normalize_code(value, "suffix")


def normalize_code_series(series: pd.Series, style: CodeStyle = "digits") -> pd.Series:
    return series.map(lambda value: normalize_code(value, style))


def read_csv_auto(
    path: str | Path,
    *,
    encodings: Sequence[str] = CSV_ENCODINGS,
    **kwargs,
) -> pd.DataFrame:
    """依次尝试常见中文 CSV 编码，并保留 pandas 的其他读取参数。"""
    if "encoding" in kwargs:
        return pd.read_csv(path, **kwargs)

    errors: list[str] = []
    for encoding in encodings:
        try:
            return pd.read_csv(path, encoding=encoding, **kwargs)
        except (UnicodeError, UnicodeDecodeError) as exc:
            errors.append(f"{encoding}: {exc}")
        except Exception as exc:
            # 错误编码有时会表现为解析错误，继续尝试其他编码。
            errors.append(f"{encoding}: {exc}")

    detail = "; ".join(errors)
    raise RuntimeError(f"CSV 读取失败：{path}；尝试编码：{detail}")


def read_table(path: str | Path, **kwargs) -> pd.DataFrame:
    """按扩展名读取 CSV 或 Excel。"""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"文件不存在：{file_path}")
    if file_path.suffix.lower() == ".csv":
        return read_csv_auto(file_path, **kwargs)
    if file_path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(file_path, **kwargs)
    raise ValueError(f"不支持的文件类型：{file_path}")


def require_columns(df: pd.DataFrame, columns: Iterable[str], label: str) -> None:
    missing = [column for column in columns if column not in df.columns]
    if missing:
        raise KeyError(f"{label} 缺少必要列：{missing}；实际列名：{list(df.columns)}")


def extract_date_tag(path: str | Path) -> str | None:
    matches = _DATE_RE.findall(Path(path).name)
    return matches[-1] if matches else None


def date_tag_from_path(path: str | Path, fallback: str | None = None) -> str:
    tag = extract_date_tag(path)
    if tag:
        return tag
    return fallback or datetime.now().strftime("%Y%m%d")


def latest_matching_file(
    directory: str | Path,
    pattern: str,
    *,
    required: bool = True,
) -> Path | None:
    """按文件名日期优先、修改时间其次选择最新文件。"""
    base_dir = Path(directory)
    candidates = [path for path in base_dir.glob(pattern) if path.is_file()]
    if not candidates:
        if required:
            raise FileNotFoundError(f"未找到匹配文件：{base_dir / pattern}")
        return None

    def sort_key(path: Path) -> tuple[str, int, str]:
        return extract_date_tag(path) or "00000000", path.stat().st_mtime_ns, path.name

    return max(candidates, key=sort_key)


def dated_output_path(
    output_dir: str | Path,
    prefix: str,
    *,
    date_tag: str | None = None,
    suffix: str = ".csv",
) -> Path:
    """生成“前缀_YYYYMMDD.扩展名”格式的输出路径。"""
    normalized_suffix = suffix if suffix.startswith(".") else f".{suffix}"
    tag = date_tag or datetime.now().strftime("%Y%m%d")
    return Path(output_dir) / f"{prefix}_{tag}{normalized_suffix}"


def timestamped_output_path(
    output_dir: str | Path,
    prefix: str,
    *,
    timestamp: str | None = None,
    suffix: str = ".xlsx",
) -> Path:
    """生成“前缀_YYYYMMDD_HHMMSS.扩展名”格式的输出路径。"""
    tag = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    return dated_output_path(output_dir, prefix, date_tag=tag, suffix=suffix)


def write_csv(df: pd.DataFrame, path: str | Path, **kwargs) -> None:
    options = {"index": False, "encoding": "utf-8-sig"}
    options.update(kwargs)
    df.to_csv(path, **options)
