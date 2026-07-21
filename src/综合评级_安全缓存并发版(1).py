# -*- coding: utf-8 -*-
"""
A股趋势分类（自动日期版）——并发进程 + 登录态保护 + 单股超时重试版

本文件是项目唯一的主评级入口。
历史评级实现已归档，不应再用于日常运行或继续开发。

核心功能：
1. 读取统一配置中的股票池
2. 自动获取最近一个交易日
3. 自动反推开始日期
4. 用 baostock 拉取历史行情
5. 加入 ADX / Donchian突破 / Relative Strength(相对沪深300)
6. 输出分类文件，格式保持和股票池一致
7. 输出分类总表，方便调参

本版修正：
1. 每只股票放到独立子进程处理
2. 单次处理超过 STOCK_TIMEOUT_SECONDS 秒，强制终止子进程
3. 超时后自动重试，不会直接归入边界模糊
4. 连续 STOCK_MAX_ATTEMPTS 次失败/超时后才跳过
5. ADX 使用 Wilder 平滑算法
6. 成交量确认改为 5日均额 / 60日均额，若无 amount 则退回 5日均量 /   均量
7. 股票使用前复权，指数使用不复权
8. 不包含标准表逻辑
9. 并发时子进程不主动 logout，避免 baostock 登录态互相踢掉导致“用户未登录”
"""

import warnings
warnings.filterwarnings("ignore")

import argparse
import time
import os
import multiprocessing as mp
from collections import deque
import queue as queue_lib
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import baostock as bs

from pipeline_config import config_value, project_path, resolve_input
from classification_rules import classify_label
from history_store import archive_run_snapshot, merge_history
from opportunity_score import add_opportunity_scores, opportunity_output
from stock_utils import dated_output_path, normalize_code, read_csv_auto, write_csv


# =========================
# 配置区
# =========================
START_DATE = None
END_DATE = None
DATE_TAG = None

# 基准指数：沪深300
BENCHMARK_CODE = "sh.000300"
DAILY_BAR_FIELDS = "date,open,high,low,close,volume,amount"
REQUIRED_DAILY_BAR_COLUMNS = {"date", "open", "high", "low", "close", "volume"}

# 向前回溯多少自然日，保证能算 MA200 / ADX / Donchian / RS
LOOKBACK_DAYS = 550

# 计算窗口
LOOKBACK_REG = 120
ADX_PERIOD = 14
ATR_PERIOD = 14

# 至少需要多少有效交易日
MIN_EFFECTIVE_ROWS = 220

# baostock 单次接口内部重试
MAX_RETRY = 3
RETRY_SLEEP_SECONDS = 2

# 单次子进程最大等待秒数
# 网络慢可以改成 60 或 90
STOCK_TIMEOUT_SECONDS = 45

# 单只股票最大尝试次数
# 3 表示：第1次 + 重试2次，总共尝试3次
STOCK_MAX_ATTEMPTS = 3

# 每次超时/失败后重试前等待秒数
STOCK_RETRY_SLEEP_SECONDS = 2

# 并发进程数
# 建议先用 4~8；baostock 偶发不稳定，太高容易接口失败/登录拥堵。
# 如果机器和网络稳定，可改为 8 或 10；如果频繁失败，改为 3 或 4。
CONCURRENT_WORKERS = int(config_value("rating", "workers", 6))

# 主进程轮询子进程状态的间隔秒数
POLL_INTERVAL_SECONDS = 0.20

# 并发启动错峰秒数：避免多个子进程同一瞬间 login/query，降低 baostock 登录拥堵
START_STAGGER_SECONDS = 0.35

# 重要：并发时不要在子进程 finally 里主动 bs.logout()
# 原因：baostock 登录态容易被其他进程 logout 影响，导致另一个进程 query 时出现“用户未登录”。
# 如必须释放，可等整批任务结束后让系统自动回收进程。
BAOSTOCK_LOGOUT_IN_CHILD = False

# 安全模式：baostock 只在主进程单线程拉数据，子进程只做本地计算
# 这样避免并发 login/query/logout 触发 baostock 黑名单或“用户未登录”。
CACHE_DIR = str(config_value("files", "rating_cache_dir", "cache/baostock"))
HISTORY_DIR = str(config_value("files", "history_dir", "data/history"))
CACHE_FORCE_REFRESH = False
BAOSTOCK_QUERY_INTERVAL_SECONDS = 0.35

# 分类列表
CATEGORIES = [
    "上升",
    "震荡上行",
    "横盘",
    "震荡下行",
    "下降",
    "筑底",
    "赶顶",
    "过渡状态",
    "边界模糊",
]


def update_classification_count_history(path, date_tag: str, counts: dict[str, int]) -> pd.DataFrame:
    history_path = project_path(path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    columns = ["日期", *[f"{category}数量" for category in CATEGORIES]]
    if history_path.exists():
        history = read_csv_auto(history_path, dtype={"日期": str}).reindex(columns=columns)
    else:
        history = pd.DataFrame(columns=columns)
    try:
        display_date = datetime.strptime(date_tag, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        display_date = str(date_tag)
    row = {"日期": display_date}
    row.update({f"{category}数量": int(counts.get(category, 0)) for category in CATEGORIES})
    history = history.loc[history["日期"].astype(str) != display_date]
    history = pd.concat([history, pd.DataFrame([row])], ignore_index=True).sort_values("日期")
    write_csv(history, history_path)
    return history


# =========================
# 工具函数
# =========================
def to_bs_code(code: str) -> str:
    return normalize_code(code, "baostock")


def clamp(x, low, high):
    return max(low, min(high, x))


def score_0_100(x, low, high):
    """
    将数值映射到 0~100
    """
    if pd.isna(x):
        return 50.0

    if high <= low:
        return 50.0

    val = (x - low) / (high - low) * 100
    return float(clamp(val, 0, 100))


def score_neg100_100(x, low, high):
    """
    将数值映射到 -100~100
    """
    if pd.isna(x):
        return 0.0

    if high <= low:
        return 0.0

    val = (x - low) / (high - low) * 200 - 100
    return float(clamp(val, -100, 100))


def calc_r2(y: np.ndarray, y_pred: np.ndarray) -> float:
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)

    if ss_tot == 0:
        return 0.0

    return float(1 - ss_res / ss_tot)


def safe_round(x, digits=2):
    try:
        if pd.isna(x):
            return np.nan
        return round(float(x), digits)
    except Exception:
        return np.nan


def get_name_from_row(row: pd.Series) -> str:
    for col in ["名称", "股票名称", "name", "Name"]:
        if col in row.index:
            val = str(row.get(col, "")).strip()
            if val and val.lower() != "nan":
                return val
    return ""


def relogin_baostock():
    """
    baostock 偶发连接异常时，尝试重新登录。

    注意：并发模式下不要先 logout 再 login。
    多个子进程同时跑时，某个进程 logout 可能会让另一个正在 query 的进程变成“用户未登录”。
    因此这里采用“只 login、不 logout”的保守策略。
    """
    lg = bs.login()

    if lg.error_code != "0":
        raise RuntimeError(f"baostock 重新登录失败: {lg.error_msg}")


def ensure_baostock_login():
    """
    在子进程内确保 baostock 已登录。
    不主动 logout，避免并发进程之间互相踢登录态。
    """
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")


def is_not_login_error(err_msg: str) -> bool:
    text = str(err_msg)
    return ("未登录" in text) or ("用户未登录" in text) or ("login" in text.lower())


def get_last_trading_date():
    """
    自动获取最近一个交易日
    """
    today = datetime.today().strftime("%Y-%m-%d")
    start_date = (datetime.today() - timedelta(days=20)).strftime("%Y-%m-%d")

    last_err = None

    for attempt in range(1, MAX_RETRY + 1):
        try:
            rs = bs.query_trade_dates(start_date=start_date, end_date=today)

            if rs.error_code != "0":
                # 并发场景下偶发“用户未登录”：通常是登录态被其他进程影响。
                # 这里立即补一次 login，然后交给外层 retry 再拉取。
                if is_not_login_error(rs.error_msg):
                    try:
                        ensure_baostock_login()
                    except Exception:
                        pass
                raise RuntimeError(rs.error_msg)

            rows = []

            while rs.next():
                rows.append(rs.get_row_data())

            if not rows:
                raise RuntimeError("无法获取交易日历")

            df = pd.DataFrame(rows, columns=rs.fields)
            df = df[df["is_trading_day"] == "1"].copy()

            if df.empty:
                raise RuntimeError("最近区间没有交易日")

            return df["calendar_date"].iloc[-1]

        except Exception as e:
            last_err = e
            print(f"[交易日历] 第 {attempt}/{MAX_RETRY} 次获取失败：{repr(e)}")

            if attempt < MAX_RETRY:
                time.sleep(RETRY_SLEEP_SECONDS)
                relogin_baostock()

    raise RuntimeError(f"获取最近交易日失败：{last_err}")


def get_start_date_by_end(end_date: str, days_back: int = LOOKBACK_DAYS):
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=days_back)
    return start_dt.strftime("%Y-%m-%d")


def fetch_bs_data(
    code: str,
    fields: str,
    start_date: str,
    end_date: str,
    adjustflag: str = "2",
    retry: int = MAX_RETRY,
) -> pd.DataFrame:
    """
    拉取 baostock 日线数据。

    adjustflag:
    1：后复权
    2：前复权
    3：不复权

    股票趋势分析使用前复权。
    指数使用不复权。
    """
    last_err = None

    for attempt in range(1, retry + 1):
        try:
            rs = bs.query_history_k_data_plus(
                code,
                fields,
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag=adjustflag,
            )

            if rs.error_code != "0":
                # 并发场景下偶发“用户未登录”：通常是登录态被其他进程影响。
                # 这里立即补一次 login，然后交给外层 retry 再拉取。
                if is_not_login_error(rs.error_msg):
                    try:
                        ensure_baostock_login()
                    except Exception:
                        pass
                raise RuntimeError(rs.error_msg)

            rows = []

            while rs.next():
                rows.append(rs.get_row_data())

            if not rows:
                return pd.DataFrame(columns=fields.split(","))

            df = pd.DataFrame(rows, columns=rs.fields)
            return df

        except Exception as e:
            last_err = e
            print(f"[行情获取失败] {code} 第 {attempt}/{retry} 次失败：{repr(e)}")

            if attempt < retry:
                time.sleep(RETRY_SLEEP_SECONDS)
                relogin_baostock()

    raise RuntimeError(f"{code} 行情获取失败：{last_err}")


def fetch_benchmark_data(start_date: str, end_date: str) -> pd.DataFrame:
    """获取可同时用于相对强弱和市场环境计算的完整沪深300日线。"""
    frame = fetch_bs_data(
        BENCHMARK_CODE,
        DAILY_BAR_FIELDS,
        start_date,
        end_date,
        adjustflag="3",
    )
    if frame.empty:
        raise RuntimeError("获取沪深300指数失败，无法计算 Relative Strength 和市场环境")

    missing = sorted(REQUIRED_DAILY_BAR_COLUMNS.difference(frame.columns))
    if missing:
        raise RuntimeError(f"沪深300行情缺少必要字段：{missing}")
    return frame




def cache_file_path(bs_code: str, start_date: str, end_date: str, adjustflag: str) -> str:
    safe_code = bs_code.replace(".", "_")
    return os.path.join(CACHE_DIR, f"{safe_code}_{start_date}_{end_date}_adj{adjustflag}.csv")


def fetch_stock_data_cached(
    stock_code: str,
    start_date: str,
    end_date: str,
    force_refresh: bool = CACHE_FORCE_REFRESH,
) -> pd.DataFrame:
    """
    主进程单线程拉取并缓存股票行情。
    注意：这个函数不要放到并发子进程里调用，避免 baostock 并发访问被限制。
    """
    bs_code = to_bs_code(stock_code)
    fp = cache_file_path(bs_code, start_date, end_date, "2")

    if (not force_refresh) and os.path.exists(fp):
        try:
            df = pd.read_csv(fp, dtype=str)
            merge_history(
                HISTORY_DIR,
                bs_code,
                df,
                kind="daily",
                source="baostock-cache",
                adjustflag="2",
            )
            return df
        except Exception:
            pass

    df = fetch_bs_data(
        bs_code,
        DAILY_BAR_FIELDS,
        start_date,
        end_date,
        adjustflag="2",
    )

    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        write_csv(df, fp)
    except Exception as e:
        print(f"[缓存写入失败] {stock_code} | {repr(e)}")

    merge_history(
        HISTORY_DIR,
        bs_code,
        df,
        kind="daily",
        source="baostock",
        adjustflag="2",
    )

    if BAOSTOCK_QUERY_INTERVAL_SECONDS > 0:
        time.sleep(BAOSTOCK_QUERY_INTERVAL_SECONDS)

    return df


def prefetch_all_stock_data(
    src_df: pd.DataFrame,
    start_date: str,
    end_date: str,
) -> dict:
    """
    单登录、单线程、限速拉取所有股票行情。
    后续分类阶段不再访问 baostock，只处理本地 DataFrame。
    """
    hist_map = {}
    total = len(src_df)

    print("[预取] 开始单线程拉取股票行情，避免 baostock 并发登录/查询触发黑名单")
    print(f"[预取] 缓存目录：{CACHE_DIR}")
    print(f"[预取] 每次查询间隔：{BAOSTOCK_QUERY_INTERVAL_SECONDS} 秒")

    for idx, (_, row) in enumerate(src_df.iterrows(), start=1):
        raw_code = str(row["代码"]).strip()
        stock_name = get_name_from_row(row)
        name_part = f" {stock_name}" if stock_name else ""
        t0 = time.time()

        try:
            hist = fetch_stock_data_cached(raw_code, start_date, end_date)
            hist_map[raw_code] = hist
            print(f"[{idx}/{total}] 行情完成：{raw_code}{name_part} | rows={len(hist)} | 用时={time.time()-t0:.2f} 秒")
        except Exception as e:
            hist_map[raw_code] = pd.DataFrame()
            print(f"[{idx}/{total}] 行情失败：{raw_code}{name_part} | {repr(e)}")

        if idx % 50 == 0 or idx == total:
            ok_count = sum(1 for v in hist_map.values() if isinstance(v, pd.DataFrame) and not v.empty)
            print(f"[预取进度] 已完成 {idx}/{total} | 有效行情 {ok_count}/{idx}")

    return hist_map


def analyze_one_stock_from_hist(
    stock_code: str,
    hist: pd.DataFrame,
    bench_df: pd.DataFrame,
):
    """
    只基于已拉取的本地行情做指标和分类，不访问 baostock。
    """
    if hist is None or hist.empty:
        return "边界模糊", "无行情数据", {}

    hist = add_basic_features(hist)

    if len(hist) < MIN_EFFECTIVE_ROWS:
        return "边界模糊", f"有效交易日不足{MIN_EFFECTIVE_ROWS}日", {}

    hist = add_atr(hist)
    hist = add_adx(hist)
    hist = add_donchian(hist)
    hist = add_relative_strength(hist, bench_df)
    hist = calc_scores(hist)

    last = hist.iloc[-1]
    label = classify_label(last)

    reason = ""
    if label == "边界模糊":
        reason = "指标边界模糊或部分指标不足"

    metrics = {
        "trend_score": safe_round(last.get("trend_score"), 2),
        "direction_score": safe_round(last.get("direction_score"), 2),
        "ma_structure_score": safe_round(last.get("ma_structure_score"), 2),
        "trend_stability_score": safe_round(last.get("trend_stability_score"), 2),

        "adx": safe_round(last.get("ADX"), 2),
        "adx_score": safe_round(last.get("adx_score"), 2),
        "breakout_score": safe_round(last.get("breakout_score"), 2),

        "RS20": safe_round(last.get("RS20"), 4),
        "RS60": safe_round(last.get("RS60"), 4),
        "rs_score": safe_round(last.get("rs_score"), 2),

        "R5": safe_round(last.get("R5"), 4),
        "R20": safe_round(last.get("R20"), 4),
        "R60": safe_round(last.get("R60"), 4),
        "R120": safe_round(last.get("R120"), 4),

        "volume_ratio": safe_round(last.get("volume_ratio"), 2),
        "volume_score": safe_round(last.get("volume_score"), 2),

        "position_score": safe_round(last.get("position_score"), 2),
        "base_score": safe_round(last.get("base_score"), 2),
        "exhaustion_score": safe_round(last.get("exhaustion_score"), 2),
        "stall_score": safe_round(last.get("stall_score"), 2),
        "stabilize_score": safe_round(last.get("stabilize_score"), 2),

        "price_ma20_dev": safe_round(last.get("price_ma20_dev"), 4),
        "price_ma60_dev": safe_round(last.get("price_ma60_dev"), 4),
        "price_ma200_dev": safe_round(last.get("price_ma200_dev"), 4),
        "ATR_ratio": safe_round(last.get("ATR_ratio"), 4),
    }

    return label, reason, metrics


# =========================
# 技术指标计算
# =========================
def add_basic_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for c in ["open", "high", "low", "close", "volume", "amount"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close", "volume"]).copy()
    df = df.sort_values("date").reset_index(drop=True)

    # 收益率
    df["ret_1"] = df["close"].pct_change()

    # 动量
    df["R5"] = df["close"] / df["close"].shift(5) - 1
    df["R10"] = df["close"] / df["close"].shift(10) - 1
    df["R20"] = df["close"] / df["close"].shift(20) - 1
    df["R60"] = df["close"] / df["close"].shift(60) - 1
    df["R120"] = df["close"] / df["close"].shift(120) - 1

    # 均线
    df["MA5"] = df["close"].rolling(5).mean()
    df["MA10"] = df["close"].rolling(10).mean()
    df["MA20"] = df["close"].rolling(20).mean()
    df["MA60"] = df["close"].rolling(60).mean()
    df["MA120"] = df["close"].rolling(120).mean()
    df["MA200"] = df["close"].rolling(200).mean()

    # 成交量 / 成交额均线
    df["VMA5"] = df["volume"].rolling(5).mean()
    df["VMA60"] = df["volume"].rolling(60).mean()

    # 优先使用成交额口径：5日均额 / 60日均额
    if "amount" in df.columns and df["amount"].notna().sum() > 0:
        df["AMT_MA5"] = df["amount"].rolling(5).mean()
        df["AMT_MA60"] = df["amount"].rolling(60).mean()
        df["volume_ratio"] = df["AMT_MA5"] / df["AMT_MA60"]
    else:
        df["volume_ratio"] = df["VMA5"] / df["VMA60"]

    # 120日通道位置
    df["rolling_120_high"] = df["high"].rolling(120).max()
    df["rolling_120_low"] = df["low"].rolling(120).min()

    rng = df["rolling_120_high"] - df["rolling_120_low"]

    df["channel_pos"] = np.where(
        rng > 0,
        (df["close"] - df["rolling_120_low"]) / rng,
        0.5
    )

    # 价格偏离
    df["price_ma20_dev"] = df["close"] / df["MA20"] - 1
    df["price_ma60_dev"] = df["close"] / df["MA60"] - 1
    df["price_ma120_dev"] = df["close"] / df["MA120"] - 1
    df["price_ma200_dev"] = df["close"] / df["MA200"] - 1

    # 高位滞涨辅助指标
    day_range = (df["high"] - df["low"]).replace(0, np.nan)
    upper_shadow = df["high"] - df[["open", "close"]].max(axis=1)

    df["upper_shadow_ratio"] = (upper_shadow / day_range).clip(lower=0, upper=1)
    df["upper_shadow_5"] = df["upper_shadow_ratio"].rolling(5).mean()

    df["high_close_gap"] = df["high"] / df["close"] - 1
    df["high_close_gap_5"] = df["high_close_gap"].rolling(5).mean()

    # 对数价格回归斜率、R2（最近120日）
    df["reg_slope"] = np.nan
    df["reg_r2"] = np.nan

    if len(df) >= LOOKBACK_REG:
        for i in range(LOOKBACK_REG - 1, len(df)):
            recent = df["close"].iloc[i - LOOKBACK_REG + 1:i + 1].values.astype(float)

            if np.any(recent <= 0):
                continue

            y = np.log(recent)
            x = np.arange(len(y), dtype=float)

            slope, intercept = np.polyfit(x, y, 1)
            y_pred = slope * x + intercept
            r2 = calc_r2(y, y_pred)

            annual_slope = slope * 244

            df.loc[i, "reg_slope"] = annual_slope
            df.loc[i, "reg_r2"] = r2

    return df


def add_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.DataFrame:
    df = df.copy()

    prev_close = df["close"].shift(1)

    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()

    df["TR"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["ATR"] = df["TR"].rolling(period).mean()
    df["ATR_ratio"] = df["ATR"] / df["close"]

    return df


def add_adx(df: pd.DataFrame, period: int = ADX_PERIOD) -> pd.DataFrame:
    """
    使用 Wilder 平滑计算 ADX。
    """
    df = df.copy()

    high = df["high"]
    low = df["low"]
    close = df["close"]

    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    move_up = high - prev_high
    move_down = prev_low - low

    plus_dm = np.where((move_up > move_down) & (move_up > 0), move_up, 0.0)
    minus_dm = np.where((move_down > move_up) & (move_down > 0), move_down, 0.0)

    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    tr_smooth = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_dm_smooth = plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    minus_dm_smooth = minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    plus_di = 100 * plus_dm_smooth / tr_smooth.replace(0, np.nan)
    minus_di = 100 * minus_dm_smooth / tr_smooth.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    df["PLUS_DI"] = plus_di
    df["MINUS_DI"] = minus_di
    df["ADX"] = adx

    return df


def add_donchian(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["DC20_high_prev"] = df["high"].rolling(20).max().shift(1)
    df["DC20_low_prev"] = df["low"].rolling(20).min().shift(1)
    df["DC60_high_prev"] = df["high"].rolling(60).max().shift(1)
    df["DC60_low_prev"] = df["low"].rolling(60).min().shift(1)

    df["breakout_20_up"] = (df["close"] > df["DC20_high_prev"]).astype(int)
    df["breakout_60_up"] = (df["close"] > df["DC60_high_prev"]).astype(int)
    df["breakout_20_down"] = (df["close"] < df["DC20_low_prev"]).astype(int)
    df["breakout_60_down"] = (df["close"] < df["DC60_low_prev"]).astype(int)

    def calc_breakout_score(row):
        if pd.isna(row["DC20_high_prev"]) or pd.isna(row["DC60_high_prev"]):
            return np.nan

        close = row["close"]

        if close > row["DC60_high_prev"]:
            return 100.0

        if close > row["DC20_high_prev"]:
            return 60.0

        if close < row["DC60_low_prev"]:
            return -100.0

        if close < row["DC20_low_prev"]:
            return -60.0

        if row["DC20_high_prev"] and row["DC20_high_prev"] != 0:
            up_dist = close / row["DC20_high_prev"] - 1
        else:
            up_dist = 0

        if row["DC20_low_prev"] and row["DC20_low_prev"] != 0:
            dn_dist = close / row["DC20_low_prev"] - 1
        else:
            dn_dist = 0

        if -0.02 <= up_dist <= 0:
            return 20.0

        if 0 <= dn_dist <= 0.02:
            return -20.0

        return 0.0

    df["breakout_score"] = df.apply(calc_breakout_score, axis=1)

    return df


def add_relative_strength(stock_df: pd.DataFrame, bench_df: pd.DataFrame) -> pd.DataFrame:
    df = stock_df.copy()
    bdf = bench_df.copy()

    bdf = bdf[["date", "close"]].copy()
    bdf["bench_close"] = pd.to_numeric(bdf["close"], errors="coerce")
    bdf = bdf.dropna(subset=["bench_close"]).copy()
    bdf = bdf.sort_values("date")

    bdf["bench_R20"] = bdf["bench_close"] / bdf["bench_close"].shift(20) - 1
    bdf["bench_R60"] = bdf["bench_close"] / bdf["bench_close"].shift(60) - 1

    bdf = bdf[["date", "bench_R20", "bench_R60"]].copy()

    df = df.merge(bdf, on="date", how="left")

    df["RS20"] = df["R20"] - df["bench_R20"]
    df["RS60"] = df["R60"] - df["bench_R60"]

    return df


# =========================
# 评分系统
# =========================
def calc_scores(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    def ma_structure_score(row):
        price = row["close"]
        ma20 = row["MA20"]
        ma60 = row["MA60"]
        ma120 = row["MA120"]
        ma200 = row["MA200"]

        if pd.isna(ma20) or pd.isna(ma60) or pd.isna(ma120) or pd.isna(ma200):
            return np.nan

        bull = 0
        bear = 0

        if price > ma20:
            bull += 20
        else:
            bear += 20

        if ma20 > ma60:
            bull += 20
        else:
            bear += 20

        if ma60 > ma120:
            bull += 25
        else:
            bear += 25

        if ma120 > ma200:
            bull += 35
        else:
            bear += 35

        return bull - bear

    df["ma_structure_score"] = df.apply(ma_structure_score, axis=1)

    # 方向分：动量 + 回归斜率
    df["direction_score"] = (
        0.15 * df["R20"].apply(lambda x: score_neg100_100(x, -0.20, 0.20)) +
        0.30 * df["R60"].apply(lambda x: score_neg100_100(x, -0.35, 0.35)) +
        0.40 * df["R120"].apply(lambda x: score_neg100_100(x, -0.50, 0.60)) +
        0.15 * df["reg_slope"].apply(lambda x: score_neg100_100(x, -0.30, 0.30))
    )

    # 长期趋势分
    df["trend_long_score"] = (
        0.20 * df["R20"].apply(lambda x: score_0_100(x, -0.10, 0.20)) +
        0.30 * df["R60"].apply(lambda x: score_0_100(x, -0.20, 0.35)) +
        0.35 * df["R120"].apply(lambda x: score_0_100(x, -0.30, 0.60)) +
        0.15 * df["reg_slope"].apply(lambda x: score_0_100(x, -0.20, 0.30))
    )

    # 趋势稳定分
    df["trend_stability_score"] = (
        0.70 * (df["reg_r2"] * 100).clip(lower=0, upper=100) +
        0.30 * df["reg_slope"].abs().apply(lambda x: score_0_100(x, 0.00, 0.25))
    )

    # ADX分
    df["adx_score"] = df["ADX"].apply(lambda x: score_0_100(x, 15, 40))

    # 波动分
    df["volatility_score"] = df["ATR_ratio"].apply(lambda x: score_0_100(x, 0.015, 0.08))

    # 成交额/成交量确认分
    df["volume_score"] = df["volume_ratio"].apply(lambda x: score_0_100(x, 0.70, 1.80))

    # 位置分
    df["position_score"] = (df["channel_pos"] * 100).clip(lower=0, upper=100)

    # RS分
    rs_raw = 0.4 * df["RS20"] + 0.6 * df["RS60"]
    df["rs_score"] = rs_raw.apply(lambda x: score_neg100_100(x, -0.20, 0.20))

    # breakout 分：从 -100~100 转成 0~100
    df["breakout_score_norm"] = df["breakout_score"].apply(
        lambda x: (x + 100) / 2 if pd.notna(x) else np.nan
    )

    # 高位滞涨分
    df["stall_score"] = (
        0.35 * (df["R20"] - df["R5"]).apply(lambda x: score_0_100(x, 0.00, 0.20)) +
        0.30 * df["upper_shadow_5"].apply(lambda x: score_0_100(x, 0.15, 0.45)) +
        0.20 * df["high_close_gap_5"].apply(lambda x: score_0_100(x, 0.01, 0.06)) +
        0.15 * np.where(df["close"] < df["MA5"], 100.0, 0.0)
    )

    # 短期企稳分
    df["stabilize_score"] = (
        0.30 * df["R5"].apply(lambda x: score_0_100(x, -0.03, 0.05)) +
        0.30 * df["R20"].apply(lambda x: score_0_100(x, -0.08, 0.08)) +
        0.20 * np.where(df["close"] > df["MA20"], 100.0, 0.0) +
        0.20 * df["RS20"].apply(lambda x: score_0_100(x, -0.08, 0.05))
    )

    # 综合趋势分
    df["trend_score"] = (
        0.18 * df["trend_long_score"] +
        0.12 * ((df["ma_structure_score"] + 100) / 2) +
        0.08 * df["trend_stability_score"] +
        0.10 * df["volume_score"] +
        0.08 * df["position_score"] +
        0.18 * df["adx_score"] +
        0.12 * df["breakout_score_norm"] +
        0.14 * ((df["rs_score"] + 100) / 2)
    )

    # 顶部衰竭分
    df["exhaustion_score"] = (
        0.25 * df["position_score"] +
        0.20 * df["price_ma60_dev"].apply(lambda x: score_0_100(x, 0.00, 0.20)) +
        0.15 * df["volatility_score"] +
        0.15 * df["volume_score"] +
        0.25 * df["stall_score"]
    )

    # 底部构建分
    df["base_score"] = (
        0.25 * (100 - df["position_score"]) +
        0.15 * (100 - df["volume_score"]) +
        0.15 * (100 - df["trend_stability_score"]) +
        0.15 * df["RS60"].apply(lambda x: score_0_100(x, -0.20, 0.05)) +
        0.15 * df["price_ma200_dev"].apply(lambda x: score_0_100(x, -0.35, 0.00)) +
        0.15 * df["stabilize_score"]
    )

    return df


# =========================
# 分类逻辑
# =========================
# =========================
# 单只股票分析
# =========================
def analyze_one_stock_inner(
    stock_code: str,
    bench_df: pd.DataFrame,
    start_date: str,
    end_date: str
):
    """
    子进程内部执行的单只股票分析逻辑。
    """
    bs_code = to_bs_code(stock_code)

    hist = fetch_bs_data(
        bs_code,
        DAILY_BAR_FIELDS,
        start_date,
        end_date,
        adjustflag="2",
    )

    if hist.empty:
        return "边界模糊", "无行情数据", {}

    hist = add_basic_features(hist)

    if len(hist) < MIN_EFFECTIVE_ROWS:
        return "边界模糊", f"有效交易日不足{MIN_EFFECTIVE_ROWS}日", {}

    hist = add_atr(hist)
    hist = add_adx(hist)
    hist = add_donchian(hist)
    hist = add_relative_strength(hist, bench_df)
    hist = calc_scores(hist)

    last = hist.iloc[-1]
    label = classify_label(last)

    reason = ""
    if label == "边界模糊":
        reason = "指标边界模糊或部分指标不足"

    metrics = {
        "trend_score": safe_round(last.get("trend_score"), 2),
        "direction_score": safe_round(last.get("direction_score"), 2),
        "ma_structure_score": safe_round(last.get("ma_structure_score"), 2),
        "trend_stability_score": safe_round(last.get("trend_stability_score"), 2),

        "adx": safe_round(last.get("ADX"), 2),
        "adx_score": safe_round(last.get("adx_score"), 2),
        "breakout_score": safe_round(last.get("breakout_score"), 2),

        "RS20": safe_round(last.get("RS20"), 4),
        "RS60": safe_round(last.get("RS60"), 4),
        "rs_score": safe_round(last.get("rs_score"), 2),

        "R5": safe_round(last.get("R5"), 4),
        "R20": safe_round(last.get("R20"), 4),
        "R60": safe_round(last.get("R60"), 4),
        "R120": safe_round(last.get("R120"), 4),

        "volume_ratio": safe_round(last.get("volume_ratio"), 2),
        "volume_score": safe_round(last.get("volume_score"), 2),

        "position_score": safe_round(last.get("position_score"), 2),
        "base_score": safe_round(last.get("base_score"), 2),
        "exhaustion_score": safe_round(last.get("exhaustion_score"), 2),
        "stall_score": safe_round(last.get("stall_score"), 2),
        "stabilize_score": safe_round(last.get("stabilize_score"), 2),

        "price_ma20_dev": safe_round(last.get("price_ma20_dev"), 4),
        "price_ma60_dev": safe_round(last.get("price_ma60_dev"), 4),
        "price_ma200_dev": safe_round(last.get("price_ma200_dev"), 4),
        "ATR_ratio": safe_round(last.get("ATR_ratio"), 4),
    }

    return label, reason, metrics


def stock_worker(
    queue,
    stock_code: str,
    hist: pd.DataFrame,
    bench_df: pd.DataFrame,
):
    """
    子进程函数：只做本地指标计算和分类，不访问 baostock。
    这样不会触发并发 login/query/logout，也不会把账号/IP打进黑名单。
    """
    try:
        label, reason, metrics = analyze_one_stock_from_hist(
            stock_code,
            hist,
            bench_df,
        )

        queue.put({
            "ok": True,
            "label": label,
            "reason": reason,
            "metrics": metrics,
        })

    except Exception as e:
        queue.put({
            "ok": False,
            "label": "边界模糊",
            "reason": f"处理异常：{repr(e)}",
            "metrics": {},
        })


def analyze_one_stock_with_timeout(
    stock_code: str,
    bench_df: pd.DataFrame,
    start_date: str,
    end_date: str
):
    """
    父进程调用。

    单只股票处理逻辑：
    1. 每次尝试都新开一个子进程
    2. 如果 STOCK_TIMEOUT_SECONDS 秒内没返回，杀掉子进程
    3. 超时后重试，最多 STOCK_MAX_ATTEMPTS 次
    4. 只有全部尝试失败/超时后，才归入边界模糊
    """
    last_reason = ""

    for attempt in range(1, STOCK_MAX_ATTEMPTS + 1):
        queue = mp.Queue()

        p = mp.Process(
            target=stock_worker,
            args=(queue, stock_code, bench_df, start_date, end_date)
        )

        p.start()
        p.join(STOCK_TIMEOUT_SECONDS)

        # 情况一：子进程超时卡死
        if p.is_alive():
            p.terminate()
            p.join()

            last_reason = (
                f"第 {attempt}/{STOCK_MAX_ATTEMPTS} 次处理超时，"
                f"超过 {STOCK_TIMEOUT_SECONDS} 秒，已终止子进程"
            )

            print(f"[超时重试] {stock_code} | {last_reason}")

            if attempt < STOCK_MAX_ATTEMPTS:
                time.sleep(STOCK_RETRY_SLEEP_SECONDS)
                continue

            return {
                "ok": False,
                "label": "边界模糊",
                "reason": (
                    f"连续 {STOCK_MAX_ATTEMPTS} 次处理超时，"
                    f"已强制跳过"
                ),
                "metrics": {},
            }

        # 情况二：子进程正常退出，并且有返回结果
        if not queue.empty():
            result = queue.get()

            # 子进程内部成功
            if result.get("ok"):
                if attempt > 1:
                    old_reason = result.get("reason", "")

                    if old_reason:
                        result["reason"] = (
                            f"前面尝试失败/超时，第 {attempt} 次重试成功；"
                            f"{old_reason}"
                        )
                    else:
                        result["reason"] = (
                            f"前面尝试失败/超时，第 {attempt} 次重试成功"
                        )

                return result

            # 子进程内部失败，不是卡死，也允许重试
            last_reason = result.get("reason", "子进程处理失败")

            print(
                f"[失败重试] {stock_code} | "
                f"第 {attempt}/{STOCK_MAX_ATTEMPTS} 次失败：{last_reason}"
            )

            if attempt < STOCK_MAX_ATTEMPTS:
                time.sleep(STOCK_RETRY_SLEEP_SECONDS)
                continue

            return {
                "ok": False,
                "label": "边界模糊",
                "reason": (
                    f"连续 {STOCK_MAX_ATTEMPTS} 次处理失败，"
                    f"最后原因：{last_reason}"
                ),
                "metrics": {},
            }

        # 情况三：子进程退出了，但 queue 没东西
        last_reason = "子进程无返回结果"

        print(
            f"[空结果重试] {stock_code} | "
            f"第 {attempt}/{STOCK_MAX_ATTEMPTS} 次无返回结果"
        )

        if attempt < STOCK_MAX_ATTEMPTS:
            time.sleep(STOCK_RETRY_SLEEP_SECONDS)
            continue

        return {
            "ok": False,
            "label": "边界模糊",
            "reason": (
                f"连续 {STOCK_MAX_ATTEMPTS} 次无返回结果，"
                f"最后原因：{last_reason}"
            ),
            "metrics": {},
        }

    return {
        "ok": False,
        "label": "边界模糊",
        "reason": f"未知异常，最后原因：{last_reason}",
        "metrics": {},
    }


def build_summary_row(
    row: pd.Series,
    label: str,
    reason: str,
    metrics: dict
) -> dict:
    summary_row = row.to_dict()
    summary_row["截止交易日"] = END_DATE
    summary_row["分类"] = label
    summary_row["备注"] = reason

    metric_cols = [
        "trend_score",
        "direction_score",
        "ma_structure_score",
        "trend_stability_score",
        "adx",
        "adx_score",
        "breakout_score",
        "RS20",
        "RS60",
        "rs_score",
        "R5",
        "R20",
        "R60",
        "R120",
        "volume_ratio",
        "volume_score",
        "position_score",
        "base_score",
        "exhaustion_score",
        "stall_score",
        "stabilize_score",
        "price_ma20_dev",
        "price_ma60_dev",
        "price_ma200_dev",
        "ATR_ratio",
    ]

    for col in metric_cols:
        summary_row[col] = metrics.get(col, np.nan)

    return summary_row



# =========================
# 并发调度逻辑
# =========================
def _safe_close_queue(q):
    try:
        q.close()
    except Exception:
        pass

    try:
        q.join_thread()
    except Exception:
        pass


def _make_final_failure(reason: str) -> dict:
    return {
        "ok": False,
        "label": "边界模糊",
        "reason": reason,
        "metrics": {},
    }


def _start_stock_process(
    idx: int,
    total: int,
    row: pd.Series,
    attempt: int,
    bench_df: pd.DataFrame,
    hist_map: dict,
) -> dict:
    raw_code = str(row["代码"]).strip()
    stock_name = get_name_from_row(row)

    if stock_name:
        print(
            f"[{idx}/{total}] 启动处理：{raw_code} {stock_name} | "
            f"第 {attempt}/{STOCK_MAX_ATTEMPTS} 次"
        )
    else:
        print(
            f"[{idx}/{total}] 启动处理：{raw_code} | "
            f"第 {attempt}/{STOCK_MAX_ATTEMPTS} 次"
        )

    q = mp.Queue()

    hist = hist_map.get(raw_code, pd.DataFrame())

    p = mp.Process(
        target=stock_worker,
        args=(q, raw_code, hist, bench_df)
    )

    p.start()

    if START_STAGGER_SECONDS > 0:
        time.sleep(START_STAGGER_SECONDS)

    return {
        "idx": idx,
        "total": total,
        "row": row,
        "attempt": attempt,
        "raw_code": raw_code,
        "stock_name": stock_name,
        "queue": q,
        "process": p,
        "start_time": time.time(),
    }


def _finish_one_result(
    task: dict,
    result: dict,
    result_map: dict,
    total_rows: list,
    finished_count: int,
) -> int:
    idx = task["idx"]
    total = task["total"]
    row = task["row"]
    raw_code = task["raw_code"]
    stock_name = task["stock_name"]
    start_time = task["start_time"]

    label = result.get("label", "边界模糊")
    reason = result.get("reason", "")
    metrics = result.get("metrics", {})

    if label not in result_map:
        label = "边界模糊"
        reason = reason or "未知分类，已归入边界模糊"

    result_map[label].append(row.to_dict())

    summary_row = build_summary_row(row, label, reason, metrics)
    total_rows.append(summary_row)

    trend = metrics.get("trend_score", np.nan)
    direction = metrics.get("direction_score", np.nan)
    adx = metrics.get("adx", np.nan)
    rs = metrics.get("rs_score", np.nan)

    name_part = f" {stock_name}" if stock_name else ""

    if reason:
        print(
            f"[{idx}/{total}] 分类完成：{raw_code}{name_part} | {label} | "
            f"trend={trend} | dir={direction} | adx={adx} | rs={rs} | "
            f"原因={reason} | 本次用时={time.time() - start_time:.2f} 秒"
        )
    else:
        print(
            f"[{idx}/{total}] 分类完成：{raw_code}{name_part} | {label} | "
            f"trend={trend} | dir={direction} | adx={adx} | rs={rs} | "
            f"本次用时={time.time() - start_time:.2f} 秒"
        )

    finished_count += 1

    if finished_count % 50 == 0 or finished_count == total:
        msg = " | ".join([f"{k}:{len(v)}" for k, v in result_map.items()])
        print(f"[进度] 已完成 {finished_count}/{total} | {msg}")

    return finished_count


def run_concurrent_analysis(
    src_df: pd.DataFrame,
    bench_df: pd.DataFrame,
    hist_map: dict,
):
    """
    多进程并发调度。

    设计目标：
    1. 同时跑多只股票，提高总速度。
    2. 每只股票仍然是独立子进程，超过 STOCK_TIMEOUT_SECONDS 就强制终止。
    3. 超时/失败后自动重试，最多 STOCK_MAX_ATTEMPTS 次。
    4. 不使用 ProcessPoolExecutor 的 future.timeout，因为它不能可靠杀掉单个卡死任务。
    """
    result_map = {cat: [] for cat in CATEGORIES}
    total_rows = []

    total = len(src_df)
    max_workers = max(1, min(int(CONCURRENT_WORKERS), total))

    pending = deque()
    for idx, (_, row) in enumerate(src_df.iterrows(), start=1):
        pending.append({
            "idx": idx,
            "row": row,
            "attempt": 1,
        })

    active = []
    finished_count = 0

    print(f"[并发] 并发进程数：{max_workers}")
    print(f"[并发] 单只股票超时：{STOCK_TIMEOUT_SECONDS} 秒")
    print(f"[并发] 单只股票最多尝试：{STOCK_MAX_ATTEMPTS} 次")

    while pending or active:
        # 补满并发槽位
        while pending and len(active) < max_workers:
            item = pending.popleft()

            task = _start_stock_process(
                idx=item["idx"],
                total=total,
                row=item["row"],
                attempt=item["attempt"],
                bench_df=bench_df,
                hist_map=hist_map,
            )

            active.append(task)

        # 检查已运行任务
        for task in list(active):
            p = task["process"]
            q = task["queue"]
            attempt = task["attempt"]
            raw_code = task["raw_code"]

            elapsed = time.time() - task["start_time"]

            # 情况一：子进程超时卡死，强制终止
            if p.is_alive() and elapsed > STOCK_TIMEOUT_SECONDS:
                try:
                    p.terminate()
                    p.join(timeout=3)
                    if p.is_alive():
                        p.kill()
                        p.join(timeout=3)
                except Exception:
                    pass

                _safe_close_queue(q)
                active.remove(task)

                reason = (
                    f"第 {attempt}/{STOCK_MAX_ATTEMPTS} 次处理超时，"
                    f"超过 {STOCK_TIMEOUT_SECONDS} 秒，已终止子进程"
                )

                print(f"[超时] {raw_code} | {reason}")

                if attempt < STOCK_MAX_ATTEMPTS:
                    pending.append({
                        "idx": task["idx"],
                        "row": task["row"],
                        "attempt": attempt + 1,
                    })
                else:
                    final_result = _make_final_failure(
                        f"连续 {STOCK_MAX_ATTEMPTS} 次处理超时，已强制跳过"
                    )
                    finished_count = _finish_one_result(
                        task,
                        final_result,
                        result_map,
                        total_rows,
                        finished_count,
                    )

                continue

            # 情况二：子进程已经退出
            if not p.is_alive():
                p.join(timeout=1)

                try:
                    result = q.get(timeout=0.5)
                except queue_lib.Empty:
                    result = _make_final_failure("子进程无返回结果")

                _safe_close_queue(q)
                active.remove(task)

                if result.get("ok"):
                    if attempt > 1:
                        old_reason = result.get("reason", "")
                        if old_reason:
                            result["reason"] = (
                                f"前面尝试失败/超时，第 {attempt} 次重试成功；"
                                f"{old_reason}"
                            )
                        else:
                            result["reason"] = (
                                f"前面尝试失败/超时，第 {attempt} 次重试成功"
                            )

                    finished_count = _finish_one_result(
                        task,
                        result,
                        result_map,
                        total_rows,
                        finished_count,
                    )
                    continue

                reason = result.get("reason", "子进程处理失败")
                print(
                    f"[失败] {raw_code} | "
                    f"第 {attempt}/{STOCK_MAX_ATTEMPTS} 次失败：{reason}"
                )

                if attempt < STOCK_MAX_ATTEMPTS:
                    pending.append({
                        "idx": task["idx"],
                        "row": task["row"],
                        "attempt": attempt + 1,
                    })
                else:
                    final_result = _make_final_failure(
                        f"连续 {STOCK_MAX_ATTEMPTS} 次处理失败，最后原因：{reason}"
                    )
                    finished_count = _finish_one_result(
                        task,
                        final_result,
                        result_map,
                        total_rows,
                        finished_count,
                    )

        if pending or active:
            time.sleep(POLL_INTERVAL_SECONDS)

    return result_map, total_rows


# =========================
# 主程序
# =========================
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="A股趋势综合评级（唯一主入口）")
    parser.add_argument("--input", help="股票池 CSV；默认使用配置中的 files.stock_pool")
    parser.add_argument(
        "--output-dir",
        default=config_value("files", "output_dir", "data/output"),
        help="结果输出目录",
    )
    parser.add_argument(
        "--cache-dir",
        default=config_value("files", "rating_cache_dir", CACHE_DIR),
        help="Baostock 缓存目录",
    )
    parser.add_argument(
        "--history-dir",
        default=config_value("files", "history_dir", HISTORY_DIR),
        help="正式历史行情目录；不会被缓存清理",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=int(config_value("rating", "workers", CONCURRENT_WORKERS)),
        help="并发分析进程数",
    )
    parser.add_argument(
        "--count-history",
        default=config_value("files", "classification_count_history", "data/output/分类数量历史.csv"),
        help="九种分类每日数量历史CSV",
    )
    return parser.parse_args(argv)


def main(argv=None):
    global START_DATE, END_DATE, DATE_TAG, CACHE_DIR, HISTORY_DIR, CONCURRENT_WORKERS

    args = parse_args(argv)
    input_file = resolve_input(args.input, config_key="stock_pool")
    output_dir = project_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    CACHE_DIR = str(project_path(args.cache_dir))
    HISTORY_DIR = str(project_path(args.history_dir))
    CONCURRENT_WORKERS = max(1, args.workers)

    src_df = pd.read_csv(input_file, dtype={"代码": str})
    src_df.columns = [str(c).strip() for c in src_df.columns]

    if "代码" not in src_df.columns:
        raise ValueError("输入文件必须包含【代码】列")

    original_columns = list(src_df.columns)

    lg = bs.login()

    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")

    try:
        END_DATE = get_last_trading_date()
        START_DATE = get_start_date_by_end(END_DATE, LOOKBACK_DAYS)
        DATE_TAG = END_DATE.replace("-", "")

        print(f"[初始化] 本次分析截止交易日：{END_DATE}")
        print(f"[初始化] 本次分析起始日期：{START_DATE}")
        print(f"[初始化] 输入文件：{input_file}")
        print(f"[初始化] 输出目录：{output_dir}")
        print(f"[初始化] 输入股票数量：{len(src_df)}")
        print(f"[初始化] 并发进程数：{CONCURRENT_WORKERS}")
        print(f"[初始化] 并发启动错峰：{START_STAGGER_SECONDS} 秒")
        print(f"[初始化] 子进程结束后主动logout：{BAOSTOCK_LOGOUT_IN_CHILD}")
        print(f"[初始化] 缓存目录：{CACHE_DIR}")
        print(f"[初始化] 正式历史库：{HISTORY_DIR}")
        print(f"[初始化] 强制刷新缓存：{CACHE_FORCE_REFRESH}")
        print(f"[初始化] baostock查询间隔：{BAOSTOCK_QUERY_INTERVAL_SECONDS} 秒")
        print(f"[初始化] 单次处理超时阈值：{STOCK_TIMEOUT_SECONDS} 秒")
        print(f"[初始化] 单只股票最大尝试次数：{STOCK_MAX_ATTEMPTS} 次")

        print("[初始化] 正在获取基准指数（沪深300）...")

        bench_df = fetch_benchmark_data(START_DATE, END_DATE)

        merge_history(
            HISTORY_DIR,
            BENCHMARK_CODE,
            bench_df,
            kind="benchmark",
            source="baostock",
            adjustflag="3",
        )

        hist_map = prefetch_all_stock_data(src_df, START_DATE, END_DATE)

    finally:
        try:
            bs.logout()
        except Exception:
            pass

    t_all = time.time()

    result_map, total_rows = run_concurrent_analysis(
        src_df=src_df,
        bench_df=bench_df,
        hist_map=hist_map,
    )

    # 输出分类文件，保持原格式
    for cat in CATEGORIES:
        out_file = dated_output_path(output_dir, f"沪深_{cat}", date_tag=DATE_TAG)
        out_df = pd.DataFrame(result_map[cat], columns=original_columns)
        write_csv(out_df, out_file)
        print(f"{out_file}：{len(out_df)} 只")

    # 输出分类总表
    total_df = pd.DataFrame(total_rows)

    # 为了方便复盘，按原输入顺序排序；如果不需要，可删除这一段
    if "代码" in total_df.columns:
        order_map = {
            str(code).strip(): i
            for i, code in enumerate(src_df["代码"].astype(str).str.strip().tolist())
        }
        total_df["_原始顺序"] = total_df["代码"].astype(str).str.strip().map(order_map)
        total_df = total_df.sort_values("_原始顺序", na_position="last").drop(columns=["_原始顺序"])

    # 分类只描述当前状态；机会评分作为独立排序层，不参与 classify_label。
    _, _, market_metrics = analyze_one_stock_from_hist(BENCHMARK_CODE, bench_df, bench_df)
    total_df = add_opportunity_scores(total_df, market_metrics=market_metrics)
    opportunity_df = opportunity_output(total_df)

    total_csv = dated_output_path(output_dir, "沪深_分类总表", date_tag=DATE_TAG)
    total_xlsx = dated_output_path(
        output_dir, "沪深_分类总表", date_tag=DATE_TAG, suffix=".xlsx"
    )
    opportunity_csv = dated_output_path(output_dir, "沪深_机会评分", date_tag=DATE_TAG)

    write_csv(total_df, total_csv)
    write_csv(opportunity_df, opportunity_csv)
    classification_counts = (
        total_df["分类"].value_counts().to_dict() if "分类" in total_df.columns else {}
    )
    count_history = update_classification_count_history(
        args.count_history, DATE_TAG, classification_counts
    )

    run_snapshot = archive_run_snapshot(
        HISTORY_DIR,
        DATE_TAG,
        pool_file=input_file,
        signals=total_df,
        rules_file=Path(__file__).with_name("classification_rules.py"),
    )

    try:
        total_df.to_excel(total_xlsx, index=False)
    except Exception as e:
        print(f"[提示] Excel 输出失败，仅保留 CSV：{repr(e)}")

    print("\n全部完成。")
    print(f"总用时：{time.time() - t_all:.2f} 秒")
    print(f"总表已输出：{total_csv}")
    print(f"机会评分已输出：{opportunity_csv}")
    print(f"总表Excel已输出：{total_xlsx}")
    print(f"分类数量历史：{project_path(args.count_history)}，共 {len(count_history)} 个日期")
    print(f"可复现运行快照：{run_snapshot}")

if __name__ == "__main__":
    # Windows 下 multiprocessing 必须放在这里
    mp.freeze_support()
    main()
