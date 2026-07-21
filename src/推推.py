# -*- coding: utf-8 -*-
"""
按配置间隔筛选 A 股股票（无浏览器实时计算版）

功能：
1. 非交易时间不执行
2. 股票池来自统一配置的 data/input
3. 新浪批量抓取实时价、涨幅、成交量和成交额
4. 本地历史K线追加当日实时快照，复用综合评级规则即时分类
5. 根据实时成交量和近5日成交量计算盘中量比
6. 根据实时价格和股票池总股本计算总市值
7. 记录命中次数到：命中次数统计.csv
8. 输出带日期的当日结果：推推_YYYYMMDD.csv
"""

import argparse
import importlib.util
import os
import re
import time
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import pandas as pd
import requests

from pipeline_config import config_value, project_path, resolve_input
from stock_utils import (
    dated_output_path,
    market_suffix,
    normalize_code_digits as normalize_code,
    read_csv_auto,
    write_csv as write_csv_utf8_sig,
)

# =========================
# 配置区
# =========================
POOL_FILE = ""
HIT_COUNT_FILE = str(
    project_path(config_value("files", "hit_count_file", "data/output/命中次数统计.csv"))
)
RESULT_OUTPUT_DIR = project_path("data/output")
RESULT_COLUMNS = [
    "代码6", "名称", "分类", "最新价", "涨幅", "成交量", "成交额",
    "量比", "总市值亿", "命中次数", "行情时间", "分类计算时间",
    "历史数据日期", "量比来源", "市值来源", "数据完整性",
]

VALID_CLASS_SET = {"上升", "震荡上行"}
KNOWN_INDEX_SYMBOLS = {
    "sh000001", "sh000016", "sh000300", "sh000905",
    "sz399001", "sz399005", "sz399006",
}

MIN_VOLUME_RATIO = 2.0
MIN_PCT = 2.0
MAX_PCT = 4.0
MAX_MARKET_CAP_YI = 2500.0

SINA_BATCH_SIZE = 200
REQUEST_TIMEOUT = 10
INTERVAL_SECONDS = int(config_value("monitor", "interval_seconds", 600))

HISTORY_DIR = project_path(config_value("files", "history_dir", "data/history"))
MIN_TREND_HISTORY_ROWS = 220

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.sina.com.cn/",
}


# =========================
# 通用函数
# =========================
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def format_interval(seconds: int) -> str:
    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes and remaining_seconds:
        return f"{seconds} 秒（{minutes} 分 {remaining_seconds} 秒）"
    if minutes:
        return f"{seconds} 秒（{minutes} 分钟）"
    return f"{seconds} 秒"


def chunk_list(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def to_float(x):
    try:
        if x is None:
            return None
        s = str(x).strip()
        if s in ("", "--", "-"):
            return None
        s = s.replace(",", "").replace("%", "").replace("亿", "").replace("万", "")
        return float(s)
    except Exception:
        return None


# =========================
# 交易时间判断
# =========================
def is_trading_time(dt=None) -> bool:
    if dt is None:
        dt = datetime.now()

    if dt.weekday() >= 5:
        return False

    hhmm = dt.hour * 100 + dt.minute
    morning = 930 <= hhmm <= 1130
    afternoon = 1300 <= hhmm <= 1500
    return morning or afternoon


# =========================
# 文件二前三列信息
# =========================
def load_pool_first3_info(pool_file: str):
    """
    读取股票池前三列，后续命中次数统计文件保持前三列一致
    返回：
    - first3_cols: 文件二前三列列名
    - prefix_df: [代码6 + 前三列] 的去重结果
    """
    df = read_csv_auto(pool_file, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]

    if len(df.columns) < 3:
        raise ValueError(f"{pool_file} 至少需要有前三列")
    if "代码" not in df.columns:
        raise ValueError(f"{pool_file} 必须包含【代码】列")

    first3_cols = list(df.columns[:3])

    df["代码6"] = df["代码"].apply(normalize_code)
    market = df["市场"] if "市场" in df.columns else pd.Series("", index=df.index)
    symbols = [
        _sina_symbol(code, market_name)
        for code, market_name in zip(df["代码"], market)
    ]
    df = df[[symbol not in KNOWN_INDEX_SYMBOLS for symbol in symbols]].copy()
    prefix_df = df[["代码6"] + first3_cols].drop_duplicates(subset=["代码6"], keep="first").copy()

    return first3_cols, prefix_df


def build_prefix_map(pool_file: str):
    first3_cols, prefix_df = load_pool_first3_info(pool_file)
    prefix_map = {}

    for _, row in prefix_df.iterrows():
        code6 = normalize_code(row["代码6"])
        prefix_map[code6] = {col: row[col] for col in first3_cols}

    return first3_cols, prefix_map


# =========================
# 命中次数统计
# =========================
def load_hit_count_df() -> pd.DataFrame:
    first3_cols, _ = build_prefix_map(POOL_FILE)
    fixed_cols = ["代码6", "名称", "命中次数", "首次命中时间", "最后命中时间"]
    all_cols = first3_cols + fixed_cols

    if not os.path.exists(HIT_COUNT_FILE):
        return pd.DataFrame(columns=all_cols)

    df = read_csv_auto(HIT_COUNT_FILE, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]

    for col in all_cols:
        if col not in df.columns:
            df[col] = ""

    df["代码6"] = df["代码6"].apply(normalize_code)
    df["名称"] = df["名称"].astype(str)
    df["命中次数"] = pd.to_numeric(df["命中次数"], errors="coerce").fillna(0).astype(int)
    df["首次命中时间"] = df["首次命中时间"].astype(str)
    df["最后命中时间"] = df["最后命中时间"].astype(str)

    return df[all_cols].copy()


def update_hit_count(result_df: pd.DataFrame):
    """
    命中次数文件前三列与股票池前三列保持一致
    """
    first3_cols, prefix_map = build_prefix_map(POOL_FILE)

    if result_df.empty:
        return

    count_df = load_hit_count_df()
    count_map = {}

    # 先读旧文件
    for _, row in count_df.iterrows():
        code6 = normalize_code(row["代码6"])

        item = {
            "代码6": code6,
            "名称": str(row["名称"]).strip(),
            "命中次数": int(row["命中次数"]),
            "首次命中时间": str(row["首次命中时间"]).strip(),
            "最后命中时间": str(row["最后命中时间"]).strip(),
        }

        for col in first3_cols:
            item[col] = row[col] if col in row else ""

        count_map[code6] = item

    current_time = now_str()

    # 更新本轮命中
    for _, row in result_df.iterrows():
        code6 = normalize_code(row["代码6"])
        name = str(row["名称"]).strip()

        prefix_vals = prefix_map.get(code6, {col: "" for col in first3_cols})

        if code6 in count_map:
            count_map[code6]["名称"] = name
            count_map[code6]["命中次数"] += 1
            count_map[code6]["最后命中时间"] = current_time
            for col in first3_cols:
                count_map[code6][col] = prefix_vals.get(col, "")
        else:
            item = {
                "代码6": code6,
                "名称": name,
                "命中次数": 1,
                "首次命中时间": current_time,
                "最后命中时间": current_time,
            }
            for col in first3_cols:
                item[col] = prefix_vals.get(col, "")
            count_map[code6] = item

    new_df = pd.DataFrame(list(count_map.values()))

    if new_df.empty:
        new_df = pd.DataFrame(columns=first3_cols + ["代码6", "名称", "命中次数", "首次命中时间", "最后命中时间"])
    else:
        new_df["命中次数"] = pd.to_numeric(new_df["命中次数"], errors="coerce").fillna(0).astype(int)
        new_df = new_df[first3_cols + ["代码6", "名称", "命中次数", "首次命中时间", "最后命中时间"]]
        new_df = new_df.sort_values(by=["命中次数", "代码6"], ascending=[False, True])

    write_csv_utf8_sig(new_df, HIT_COUNT_FILE)


def attach_hit_count(result_df: pd.DataFrame) -> pd.DataFrame:
    if result_df.empty:
        result_df = result_df.copy()
        result_df["命中次数"] = pd.Series(dtype="int64")
        return result_df

    count_df = load_hit_count_df()
    merged = result_df.merge(count_df[["代码6", "命中次数"]], on="代码6", how="left")
    merged["命中次数"] = pd.to_numeric(merged["命中次数"], errors="coerce").fillna(0).astype(int)
    return merged


# =========================
# 本地文件
# =========================
def _sina_symbol(raw_code: str, market: str = "") -> str:
    raw = str(raw_code).strip().upper()
    code6 = normalize_code(raw)
    if raw.endswith(".SH") or str(market).strip() == "上海":
        return f"sh{code6}"
    if raw.endswith(".SZ") or str(market).strip() == "深圳":
        return f"sz{code6}"
    if raw.endswith(".BJ") or str(market).strip() == "北京":
        return f"bj{code6}"
    return f"{market_suffix(code6).lower()}{code6}"


def load_pool_info(pool_file: str) -> pd.DataFrame:
    df = read_csv_auto(pool_file, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    if "代码" not in df.columns:
        raise ValueError(f"{pool_file} 中未找到【代码】列")

    df["代码6"] = df["代码"].apply(normalize_code)
    df = df[df["代码6"].str.fullmatch(r"\d{6}", na=False)].copy()
    market = df["市场"] if "市场" in df.columns else pd.Series("", index=df.index)
    df["新浪代码"] = [
        _sina_symbol(code, market_name)
        for code, market_name in zip(df["代码"], market)
    ]
    df = df[~df["新浪代码"].isin(KNOWN_INDEX_SYMBOLS)].copy()
    for column in ("名称", "总股本"):
        if column not in df.columns:
            df[column] = ""
    return df[["代码6", "名称", "新浪代码", "总股本"]].drop_duplicates(
        subset=["代码6"], keep="first"
    )


# =========================
# 新浪批量实时行情
# =========================
def _normalize_sina_volume(price, volume, amount):
    """新浪股票成交量在部分行情中以手返回，依据成交额自动统一为股。"""
    price_value = to_float(price)
    volume_value = to_float(volume)
    amount_value = to_float(amount)
    if not price_value or not volume_value:
        return volume_value
    if amount_value:
        unit_ratio = amount_value / (price_value * volume_value)
        if 50 <= unit_ratio <= 150:
            return volume_value * 100.0
    return volume_value


def fetch_sina_realtime(pool_info: pd.DataFrame) -> pd.DataFrame:
    all_rows = []
    session = requests.Session()
    session.headers.update(HEADERS)

    records = pool_info[["代码6", "新浪代码"]].to_dict(orient="records")
    for batch in chunk_list(records, SINA_BATCH_SIZE):
        symbols = ",".join(item["新浪代码"] for item in batch)
        url = f"https://hq.sinajs.cn/list={symbols}"

        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()

        text = resp.content.decode("gbk", errors="ignore")
        pattern = re.compile(r'var hq_str_((?:sh|sz|bj)\d{6})="([^"]*)";')
        matches = pattern.findall(text)

        for symbol, content in matches:
            parts = content.split(",")
            if len(parts) < 10:
                continue

            name = parts[0].strip()
            last_close = to_float(parts[2])
            price = to_float(parts[3])
            open_price = to_float(parts[1])
            high_price = to_float(parts[4])
            low_price = to_float(parts[5])
            amount_yuan = to_float(parts[9])
            volume_shares = _normalize_sina_volume(price, parts[8], amount_yuan)
            quote_date = parts[30].strip() if len(parts) > 30 else datetime.now().strftime("%Y-%m-%d")
            quote_time = parts[31].strip() if len(parts) > 31 else datetime.now().strftime("%H:%M:%S")

            code6 = symbol[-6:]
            pct = None
            if price is not None and last_close not in (None, 0):
                pct = (price - last_close) / last_close * 100.0

            all_rows.append({
                "代码6": code6,
                "名称": name,
                "最新价": price,
                "昨收": last_close,
                "今开": open_price,
                "最高": high_price,
                "最低": low_price,
                "涨幅": pct,
                "成交量": volume_shares,
                "成交额": amount_yuan,
                "行情日期": quote_date,
                "行情时间": f"{quote_date} {quote_time}".strip(),
            })

        time.sleep(0.2)

    return pd.DataFrame(all_rows)


# =========================
# 本地历史与实时分类
# =========================
@lru_cache(maxsize=1)
def load_rating_engine():
    path = Path(__file__).with_name("综合评级_安全缓存并发版(1).py")
    spec = importlib.util.spec_from_file_location("tuitui_rating_engine", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载评级引擎：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def elapsed_trading_minutes(current_time: datetime | None = None) -> int:
    current = current_time or datetime.now()
    minutes = current.hour * 60 + current.minute
    morning_start = 9 * 60 + 30
    morning_end = 11 * 60 + 30
    afternoon_start = 13 * 60
    afternoon_end = 15 * 60
    if minutes <= morning_start:
        return 0
    if minutes <= morning_end:
        return minutes - morning_start
    if minutes < afternoon_start:
        return 120
    if minutes <= afternoon_end:
        return 120 + minutes - afternoon_start
    return 240


def load_local_history(code6: str, market_prefix: str, *, benchmark: bool = False) -> pd.DataFrame:
    kind = "benchmark" if benchmark else "daily"
    path = HISTORY_DIR / kind / market_prefix / f"{code6}.csv"
    if not path.is_file():
        return pd.DataFrame()
    return read_csv_auto(path, dtype=str)


def _complete_history(history: pd.DataFrame, quote_date: str) -> pd.DataFrame:
    if history.empty or "date" not in history.columns:
        return history.copy()
    dates = pd.to_datetime(history["date"], errors="coerce")
    return history[dates < pd.Timestamp(quote_date)].copy()


def intraday_volume_ratio(
    history: pd.DataFrame,
    current_volume,
    current_time: datetime | None = None,
    quote_date: str | None = None,
) -> float | None:
    elapsed = elapsed_trading_minutes(current_time)
    volume = to_float(current_volume)
    if elapsed < 5 or volume is None or volume <= 0 or history.empty:
        return None
    completed = _complete_history(history, quote_date or (current_time or datetime.now()).strftime("%Y-%m-%d"))
    if "volume" not in completed.columns:
        return None
    average = pd.to_numeric(completed["volume"], errors="coerce").dropna().tail(5).mean()
    if pd.isna(average) or average <= 0:
        return None
    return float(volume / (average * elapsed / 240.0))


def append_realtime_bar(
    history: pd.DataFrame,
    quote: pd.Series,
    current_time: datetime | None = None,
) -> pd.DataFrame:
    quote_date = str(quote.get("行情日期") or (current_time or datetime.now()).strftime("%Y-%m-%d"))
    completed = _complete_history(history, quote_date)
    elapsed = max(5, elapsed_trading_minutes(current_time))
    projection = 240.0 / elapsed
    close = to_float(quote.get("最新价"))
    if close is None:
        raise ValueError("实时行情缺少最新价，无法追加当日快照")
    previous = to_float(quote.get("昨收")) or close
    open_price = to_float(quote.get("今开")) or previous
    high = to_float(quote.get("最高")) or max(value for value in (open_price, close) if value is not None)
    low = to_float(quote.get("最低")) or min(value for value in (open_price, close) if value is not None)
    row = pd.DataFrame([{
        "date": quote_date,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": (to_float(quote.get("成交量")) or 0) * projection,
        "amount": (to_float(quote.get("成交额")) or 0) * projection,
    }])
    return pd.concat([completed, row], ignore_index=True)


def realtime_market_cap(pool_row: pd.Series, price) -> tuple[float | None, str]:
    current_price = to_float(price)
    total_shares = to_float(pool_row.get("总股本"))
    if current_price and total_shares and total_shares > 0:
        return current_price * total_shares / 100_000_000.0, "实时价×股票池总股本"
    return None, "缺失"


def build_realtime_table(pool_info: pd.DataFrame, current_time: datetime | None = None) -> pd.DataFrame:
    now = current_time or datetime.now()
    sina_df = fetch_sina_realtime(pool_info)
    if sina_df.empty:
        return sina_df
    pre_df = sina_df[
        sina_df["涨幅"].notna()
        & (sina_df["涨幅"] >= MIN_PCT)
        & (sina_df["涨幅"] <= MAX_PCT)
    ].merge(pool_info, on="代码6", how="left", suffixes=("", "_股票池"))
    if pre_df.empty:
        return pre_df

    benchmark_pool = pd.DataFrame([{"代码6": "000300", "新浪代码": "sh000300"}])
    benchmark_quote = fetch_sina_realtime(benchmark_pool)
    benchmark_history = load_local_history("000300", "sh", benchmark=True)
    if benchmark_quote.empty or benchmark_history.empty:
        raise RuntimeError("缺少沪深300实时行情或本地历史，无法实时计算相对强弱分类")
    live_benchmark = append_realtime_bar(benchmark_history, benchmark_quote.iloc[0], now)

    engine = load_rating_engine()
    rows = []
    total = len(pre_df)
    calculation_time = now.strftime("%Y-%m-%d %H:%M:%S")
    for idx, (_, quote) in enumerate(pre_df.iterrows(), start=1):
        code6 = quote["代码6"]
        market_prefix = str(quote.get("新浪代码", ""))[:2]
        print(f"[{now_str()}] 实时趋势计算 {idx}/{total}: {code6}", flush=True)
        history = load_local_history(code6, market_prefix)
        quote_date = str(quote.get("行情日期") or now.strftime("%Y-%m-%d"))
        completed = _complete_history(history, quote_date)
        history_date = str(completed["date"].iloc[-1]) if not completed.empty else ""
        volume_ratio = intraday_volume_ratio(history, quote.get("成交量"), now, quote_date)
        market_cap, market_cap_source = realtime_market_cap(quote, quote.get("最新价"))
        issues = []
        label = "边界模糊"
        if len(completed) < MIN_TREND_HISTORY_ROWS:
            issues.append(f"历史不足{MIN_TREND_HISTORY_ROWS}日")
        else:
            live_history = append_realtime_bar(history, quote, now)
            label, reason, _ = engine.analyze_one_stock_from_hist(code6, live_history, live_benchmark)
            if reason:
                issues.append(reason)
        if volume_ratio is None:
            issues.append("量比不可用")
        if market_cap is None:
            issues.append("市值不可用")
        item = quote.to_dict()
        item.update({
            "名称": quote.get("名称") or quote.get("名称_股票池", ""),
            "分类": label,
            "量比": volume_ratio,
            "总市值亿": market_cap,
            "分类计算时间": calculation_time,
            "历史数据日期": history_date,
            "量比来源": "新浪实时成交量+本地近5日" if volume_ratio is not None else "缺失",
            "市值来源": market_cap_source,
            "数据完整性": "完整" if not issues else "；".join(issues),
        })
        rows.append(item)
    return pd.DataFrame(rows)


def filter_stocks(rt_df: pd.DataFrame) -> pd.DataFrame:
    if rt_df.empty:
        return rt_df

    df = rt_df[
        (rt_df["分类"].isin(VALID_CLASS_SET)) &
        (rt_df["量比"].notna()) &
        (rt_df["总市值亿"].notna()) &
        (rt_df["量比"] > MIN_VOLUME_RATIO) &
        (rt_df["总市值亿"] < MAX_MARKET_CAP_YI) &
        (rt_df["涨幅"] >= MIN_PCT) &
        (rt_df["涨幅"] <= MAX_PCT)
    ].copy()

    df = df.sort_values(by=["涨幅", "量比"], ascending=[False, False])
    return df


# =========================
# 输出
# =========================
def build_message(df: pd.DataFrame) -> str:
    current_time = now_str()

    if df.empty:
        return f"[{current_time}] 条件命中 0 只股票。"

    lines = [f"[{current_time}] 条件命中 {len(df)} 只股票："]
    for _, row in df.iterrows():
        line = (
            f"{row['代码6']} {row['名称']}"
            f" | 分类:{row['分类']}"
            f" | 涨幅:{row['涨幅']:.2f}%"
            f" | 量比:{row['量比']:.2f}"
            f" | 总市值:{row['总市值亿']:.2f}亿"
            f" | 命中次数:{int(row['命中次数'])}"
        )
        if pd.notna(row["最新价"]):
            line += f" | 最新价:{row['最新价']:.2f}"
        lines.append(line)

    return "\n".join(lines)


def daily_result_path(
    current_time: datetime | None = None,
    output_dir: str | Path | None = None,
) -> Path:
    """生成推推当日结果路径，例如：推推_20260720.csv。"""
    date_tag = (current_time or datetime.now()).strftime("%Y%m%d")
    return dated_output_path(output_dir or RESULT_OUTPUT_DIR, "推推", date_tag=date_tag)


def write_daily_result(
    result_df: pd.DataFrame,
    current_time: datetime | None = None,
    output_dir: str | Path | None = None,
) -> Path:
    """把本轮首次出现的股票追加到当日文件，保留已有行及其顺序。"""
    output = result_df.copy()
    for column in RESULT_COLUMNS:
        if column not in output.columns:
            output[column] = ""
    output = output[RESULT_COLUMNS]

    # 同一轮可能因上游合并产生重复代码，只保留首次出现的有效股票。
    output_keys = output["代码6"].map(normalize_code)
    output = output.loc[output_keys.ne("") & ~output_keys.duplicated()].copy()
    output["代码6"] = output["代码6"].map(normalize_code)

    path = daily_result_path(current_time=current_time, output_dir=output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        write_csv_utf8_sig(output, path)
        print(
            f"[{now_str()}] 当日累计文件已创建：{path}（新增 {len(output)} 只，累计 {len(output)} 只）",
            flush=True,
        )
        return path

    existing = read_csv_auto(path, dtype=str).fillna("")
    if list(existing.columns) != RESULT_COLUMNS:
        # 兼容旧版本文件：补齐标准列并保留原有行，迁移后再进入追加模式。
        for column in RESULT_COLUMNS:
            if column not in existing.columns:
                existing[column] = ""
        existing = existing[RESULT_COLUMNS]
        write_csv_utf8_sig(existing, path)
        print(f"[{now_str()}] 已将旧版当日文件迁移为当前字段格式：{path}", flush=True)

    existing_codes = {
        code for code in existing["代码6"].map(normalize_code).tolist() if code
    }
    new_rows = output.loc[~output["代码6"].isin(existing_codes)]
    total_count = len(existing) + len(new_rows)

    if new_rows.empty:
        print(
            f"[{now_str()}] 本轮无新增股票，保留当日累计文件：{path}（累计 {len(existing)} 只）",
            flush=True,
        )
        return path

    # 文件首行已经带 UTF-8 BOM；追加时使用普通 UTF-8，避免在文件中间写入第二个 BOM。
    write_csv_utf8_sig(new_rows, path, mode="a", header=False, encoding="utf-8")
    print(
        f"[{now_str()}] 本轮新增 {len(new_rows)} 只，已追加到：{path}（累计 {total_count} 只）",
        flush=True,
    )
    return path


# =========================
# 单次执行
# =========================
def run_once(current_time: datetime | None = None):
    current = current_time or datetime.now()
    if not is_trading_time(current):
        print(f"[{now_str()}] 当前非交易时间，跳过本轮。", flush=True)
        print("-" * 100, flush=True)
        return
    if elapsed_trading_minutes(current) < 5:
        print(f"[{now_str()}] 开盘不足5分钟，实时量比不稳定，跳过本轮。", flush=True)
        print("-" * 100, flush=True)
        return

    pool_info = load_pool_info(POOL_FILE)

    print(f"[{now_str()}] 股票池数量: {len(pool_info)}", flush=True)
    print(f"[{now_str()}] 先按实时涨幅 {MIN_PCT:.1f}%—{MAX_PCT:.1f}% 初筛，再即时重算趋势分类", flush=True)

    if pool_info.empty:
        print(f"[{now_str()}] 候选股票为空", flush=True)
        write_daily_result(pd.DataFrame())
        print("-" * 100, flush=True)
        return

    rt_df = build_realtime_table(pool_info, current)
    if rt_df.empty:
        print(f"[{now_str()}] 初筛后无股票命中涨幅区间", flush=True)
        write_daily_result(pd.DataFrame())
        print("-" * 100, flush=True)
        return

    result_df = filter_stocks(rt_df)
    update_hit_count(result_df)
    result_df = attach_hit_count(result_df)
    write_daily_result(result_df)

    print(build_message(result_df), flush=True)
    print("-" * 100, flush=True)


# =========================
# 主程序
# =========================
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="盘中强势股票监控",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--pool", help="股票池 CSV；默认使用统一配置")
    parser.add_argument("--interval", type=int, default=INTERVAL_SECONDS, help="轮询间隔秒数")
    parser.add_argument("--once", action="store_true", help="只执行一轮后退出")
    return parser.parse_args(argv)


def main(argv=None):
    global POOL_FILE, INTERVAL_SECONDS

    args = parse_args(argv)
    POOL_FILE = str(resolve_input(args.pool, config_key="stock_pool"))
    INTERVAL_SECONDS = max(1, args.interval)

    print(f"股票池：{POOL_FILE}", flush=True)
    print("分类方式：本地历史K线 + 当天实时行情即时重算（不读取分类总表）", flush=True)

    print(f"开始监控，轮询间隔 {format_interval(INTERVAL_SECONDS)}...\n", flush=True)
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[{now_str()}] 执行异常: {e}", flush=True)
            print("-" * 100, flush=True)
            if args.once:
                raise

        if args.once:
            break
        print(f"[{now_str()}] 本轮结束，{format_interval(INTERVAL_SECONDS)}后开始下一轮。", flush=True)
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
