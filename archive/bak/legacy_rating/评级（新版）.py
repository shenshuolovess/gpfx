# -*- coding: utf-8 -*-
"""
A股趋势分类（自动日期版）——并发进程 + 登录态保护 + 单股超时重试版

核心功能：
1. 读取 沪深.csv
2. 自动获取最近一个交易日
3. 自动反推开始日期
4. 用 baostock 拉取历史行情
5. 加入 ADX / Donchian突破 / Relative Strength(相对沪深300)
6. 输出分类文件，格式保持和 沪深.csv 一致
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

import time
import multiprocessing as mp
from collections import deque
import queue as queue_lib
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import baostock as bs


# =========================
# 配置区
# =========================
INPUT_FILE = "沪深.csv"

START_DATE = None
END_DATE = None
DATE_TAG = None

# 基准指数：沪深300
BENCHMARK_CODE = "sh.000300"

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
CONCURRENT_WORKERS = 6

# 主进程轮询子进程状态的间隔秒数
POLL_INTERVAL_SECONDS = 0.20

# 并发启动错峰秒数：避免多个子进程同一瞬间 login/query，降低 baostock 登录拥堵
START_STAGGER_SECONDS = 0.35

# 重要：并发时不要在子进程 finally 里主动 bs.logout()
# 原因：baostock 登录态容易被其他进程 logout 影响，导致另一个进程 query 时出现“用户未登录”。
# 如必须释放，可等整批任务结束后让系统自动回收进程。
BAOSTOCK_LOGOUT_IN_CHILD = False

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


# =========================
# 工具函数
# =========================
def to_bs_code(code: str) -> str:
    """
    将:
    000001.SZ / 600519.SH / 000001 / 600519
    转为 baostock 格式:
    sz.000001 / sh.600519
    """
    code = str(code).strip()

    if code.startswith(("sh.", "sz.")):
        return code

    if "." in code:
        left, right = code.split(".", 1)
        left = left.zfill(6)
        right = right.upper()

        if right == "SH":
            return f"sh.{left}"
        elif right == "SZ":
            return f"sz.{left}"

    code = code.zfill(6)

    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return f"sh.{code}"

    return f"sz.{code}"


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
def classify_label(last_row: pd.Series) -> str:
    """
    趋势状态分类。

    本版只做分类逻辑的最小修正：
    1. 删除 high_level_stall 对“赶顶”的硬性限制，避免极强趋势高位股落入边界模糊。
    2. 将“赶顶”的 exhaustion_score 门槛从 74 提高到 82，避免普通强趋势过早归为赶顶。
    3. 将“上升”的 exhaustion_score 上限从 82 放宽到 88，避免主升浪因过热被打成边界模糊。
    4. “筑底”保留短期企稳确认，避免阴跌股误判筑底。
    """
    needed = [
        "trend_score",
        "direction_score",
        "trend_stability_score",
        "adx_score",
        "position_score",
        "rs_score",
        "breakout_score",
        "base_score",
        "exhaustion_score",
        "ma_structure_score",
        "stabilize_score",
        "R20",
        "RS20",
        "MA20",
        "close",
    ]

    if any(pd.isna(last_row.get(c)) for c in needed):
        return "边界模糊"

    trend_score = float(last_row["trend_score"])
    direction_score = float(last_row["direction_score"])
    trend_stability = float(last_row["trend_stability_score"])
    adx_score = float(last_row["adx_score"])
    position_score = float(last_row["position_score"])
    rs_score = float(last_row["rs_score"])
    breakout_score = float(last_row["breakout_score"])
    base_score = float(last_row["base_score"])
    exhaustion_score = float(last_row["exhaustion_score"])
    ma_structure_score = float(last_row["ma_structure_score"])
    stabilize_score = float(last_row["stabilize_score"])

    r20 = float(last_row["R20"])
    rs20 = float(last_row["RS20"])
    close = float(last_row["close"])
    ma20 = float(last_row["MA20"])

    # 短期企稳条件：过滤阴跌股误判筑底
    short_stabilized = (
        stabilize_score >= 58
        and close > ma20
        and r20 > -0.02
        and rs20 > -0.06
    )

    # 1. 赶顶
    # 定义为“高位 + 过热 + 强趋势”，不是必须已经滞涨。
    # 这样鼎泰高科这类极端强势高位股不会落入边界模糊。
    if (
        position_score >= 88
        and exhaustion_score >= 82
        and trend_score >= 65
        and direction_score > 20
    ):
        return "赶顶"

    # 2. 筑底
    if (
        position_score <= 35
        and base_score >= 68
        and adx_score <= 50
        and direction_score > -45
        and short_stabilized
    ):
        return "筑底"

    # 3. 上升
    # 将 exhaustion_score 上限放宽到 88，避免强趋势主升浪因为过热直接掉入边界模糊。
    if (
        trend_score >= 72
        and direction_score >= 28
        and adx_score >= 55
        and rs_score >= 15
        and (breakout_score >= 60 or ma_structure_score >= 50)
        and exhaustion_score < 88
    ):
        return "上升"

    # 4. 下降
    if (
        trend_score <= 32
        and direction_score <= -28
        and adx_score >= 50
        and rs_score <= -15
        and (breakout_score <= -60 or ma_structure_score <= -50)
    ):
        return "下降"

    # 5. 震荡上行
    if (
        52 <= trend_score < 72
        and direction_score >= 10
        and rs_score >= 0
        and breakout_score > -20
    ):
        return "震荡上行"

    # 6. 震荡下行
    if (
        30 < trend_score <= 48
        and direction_score <= -10
        and rs_score <= 5
    ):
        return "震荡下行"

    # 7. 横盘
    if (
        40 <= trend_score <= 58
        and abs(direction_score) < 18
        and adx_score <= 45
        and abs(breakout_score) <= 20
        and trend_stability < 55
    ):
        return "横盘"

    # 8. 过渡状态
    if (
        35 <= trend_score <= 68
        and (
            abs(direction_score) >= 18
            or abs(breakout_score) >= 20
            or abs(rs_score) >= 15
        )
    ):
        return "过渡状态"

    return "边界模糊"

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
        "date,open,high,low,close,volume,amount",
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
    bench_df: pd.DataFrame,
    start_date: str,
    end_date: str
):
    """
    子进程函数。

    每个子进程单独登录 baostock。
    即使某只股票卡死，父进程也可以直接杀掉子进程。
    """
    try:
        lg = bs.login()

        if lg.error_code != "0":
            raise RuntimeError(f"baostock 子进程登录失败: {lg.error_msg}")

        label, reason, metrics = analyze_one_stock_inner(
            stock_code,
            bench_df,
            start_date,
            end_date
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

    finally:
        # 并发模式下不要主动 logout。
        # 否则可能让其他正在运行的子进程 query 时出现“用户未登录”。
        if BAOSTOCK_LOGOUT_IN_CHILD:
            try:
                bs.logout()
            except Exception:
                pass


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
    start_date: str,
    end_date: str
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

    p = mp.Process(
        target=stock_worker,
        args=(q, raw_code, bench_df, start_date, end_date)
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
    start_date: str,
    end_date: str
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
                start_date=start_date,
                end_date=end_date,
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
def main():
    global START_DATE, END_DATE, DATE_TAG

    src_df = pd.read_csv(INPUT_FILE, dtype={"代码": str})
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
        print(f"[初始化] 输入股票数量：{len(src_df)}")
        print(f"[初始化] 并发进程数：{CONCURRENT_WORKERS}")
        print(f"[初始化] 并发启动错峰：{START_STAGGER_SECONDS} 秒")
        print(f"[初始化] 子进程结束后主动logout：{BAOSTOCK_LOGOUT_IN_CHILD}")
        print(f"[初始化] 单次处理超时阈值：{STOCK_TIMEOUT_SECONDS} 秒")
        print(f"[初始化] 单只股票最大尝试次数：{STOCK_MAX_ATTEMPTS} 次")

        print("[初始化] 正在获取基准指数（沪深300）...")

        bench_df = fetch_bs_data(
            BENCHMARK_CODE,
            "date,close",
            START_DATE,
            END_DATE,
            adjustflag="3",
        )

        if bench_df.empty:
            raise RuntimeError("获取沪深300指数失败，无法计算 Relative Strength")

    finally:
        try:
            bs.logout()
        except Exception:
            pass

    t_all = time.time()

    result_map, total_rows = run_concurrent_analysis(
        src_df=src_df,
        bench_df=bench_df,
        start_date=START_DATE,
        end_date=END_DATE,
    )

    # 输出分类文件，保持原格式
    for cat in CATEGORIES:
        out_file = f"沪深_{cat}_{DATE_TAG}.csv"
        out_df = pd.DataFrame(result_map[cat], columns=original_columns)
        out_df.to_csv(out_file, index=False, encoding="utf-8-sig")
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

    total_csv = f"沪深_分类总表_{DATE_TAG}.csv"
    total_xlsx = f"沪深_分类总表_{DATE_TAG}.xlsx"

    total_df.to_csv(total_csv, index=False, encoding="utf-8-sig")

    try:
        total_df.to_excel(total_xlsx, index=False)
    except Exception as e:
        print(f"[提示] Excel 输出失败，仅保留 CSV：{repr(e)}")

    print("\n全部完成。")
    print(f"总用时：{time.time() - t_all:.2f} 秒")
    print(f"总表已输出：{total_csv}")
    print(f"总表Excel已输出：{total_xlsx}")

if __name__ == "__main__":
    # Windows 下 multiprocessing 必须放在这里
    mp.freeze_support()
    main()