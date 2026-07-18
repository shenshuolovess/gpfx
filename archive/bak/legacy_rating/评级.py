# -*- coding: utf-8 -*-
"""
A股趋势分类（自动日期版）
功能：
1. 读取 沪深.csv
2. 自动获取最近一个交易日，无需手工改日期
3. 自动反推开始日期
4. 用 baostock 拉取历史行情
5. 加入 ADX / Donchian突破 / Relative Strength(相对沪深300)
6. 输出分类文件（格式保持和沪深.csv一致）
7. 输出一个分类总表，方便调参
"""

import warnings
warnings.filterwarnings("ignore")

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import baostock as bs


# =========================
# 配置区
# =========================
INPUT_FILE = "沪深.csv"

# 自动日期：无需手工改
START_DATE = None
END_DATE = None
DATE_TAG = None

# 基准指数：沪深300
BENCHMARK_CODE = "sh.000300"

# 向前回溯多少天，保证能算 MA200 / ADX / Donchian / RS
LOOKBACK_DAYS = 550

# 计算窗口
LOOKBACK_REG = 120
ADX_PERIOD = 14
ATR_PERIOD = 14

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


def max_drawdown(arr: np.ndarray) -> float:
    if len(arr) == 0:
        return 0.0
    cummax = np.maximum.accumulate(arr)
    dd = arr / cummax - 1.0
    return float(np.min(dd))


def get_last_trading_date():
    """
    自动获取最近一个交易日
    """
    today = datetime.today().strftime("%Y-%m-%d")
    start_date = (datetime.today() - timedelta(days=20)).strftime("%Y-%m-%d")

    rs = bs.query_trade_dates(start_date=start_date, end_date=today)
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


def get_start_date_by_end(end_date: str, days_back: int = LOOKBACK_DAYS):
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=days_back)
    return start_dt.strftime("%Y-%m-%d")


def fetch_bs_data(code: str, fields: str, start_date: str, end_date: str) -> pd.DataFrame:
    rs = bs.query_history_k_data_plus(
        code,
        fields,
        start_date=start_date,
        end_date=end_date,
        frequency="d",
        adjustflag="2",  # 前复权
    )

    if rs.error_code != "0":
        raise RuntimeError(rs.error_msg)

    rows = []
    while rs.next():
        rows.append(rs.get_row_data())

    if not rows:
        return pd.DataFrame(columns=fields.split(","))

    df = pd.DataFrame(rows, columns=rs.fields)
    return df


# =========================
# 技术指标计算
# =========================
def add_basic_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for c in ["open", "high", "low", "close", "volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["high", "low", "close", "volume"]).copy()
    df = df.sort_values("date").reset_index(drop=True)

    # 收益率
    df["ret_1"] = df["close"].pct_change()

    # 动量
    df["R20"] = df["close"] / df["close"].shift(20) - 1
    df["R60"] = df["close"] / df["close"].shift(60) - 1
    df["R120"] = df["close"] / df["close"].shift(120) - 1

    # 均线
    df["MA20"] = df["close"].rolling(20).mean()
    df["MA60"] = df["close"].rolling(60).mean()
    df["MA120"] = df["close"].rolling(120).mean()
    df["MA200"] = df["close"].rolling(200).mean()

    # 成交量均线
    df["VMA20"] = df["volume"].rolling(20).mean()
    df["VMA60"] = df["volume"].rolling(60).mean()
    df["volume_ratio"] = df["VMA20"] / df["VMA60"]

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
    df["price_ma60_dev"] = df["close"] / df["MA60"] - 1
    df["price_ma120_dev"] = df["close"] / df["MA120"] - 1
    df["price_ma200_dev"] = df["close"] / df["MA200"] - 1

    # 对数价格回归斜率、R2（最近120日）
    df["reg_slope"] = np.nan
    df["reg_r2"] = np.nan

    if len(df) >= LOOKBACK_REG:
        for i in range(LOOKBACK_REG - 1, len(df)):
            recent = df["close"].iloc[i - LOOKBACK_REG + 1:i + 1].values.astype(float)
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

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    tr_n = tr.rolling(period).sum()
    plus_dm_n = pd.Series(plus_dm).rolling(period).sum()
    minus_dm_n = pd.Series(minus_dm).rolling(period).sum()

    plus_di = 100 * (plus_dm_n / tr_n.replace(0, np.nan))
    minus_di = 100 * (minus_dm_n / tr_n.replace(0, np.nan))

    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.rolling(period).mean()

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

        if row["close"] > row["DC60_high_prev"]:
            return 100.0
        if row["close"] > row["DC20_high_prev"]:
            return 60.0
        if row["close"] < row["DC60_low_prev"]:
            return -100.0
        if row["close"] < row["DC20_low_prev"]:
            return -60.0

        up_dist = (row["close"] / row["DC20_high_prev"] - 1) if row["DC20_high_prev"] and row["DC20_high_prev"] != 0 else 0
        dn_dist = (row["close"] / row["DC20_low_prev"] - 1) if row["DC20_low_prev"] and row["DC20_low_prev"] != 0 else 0

        if abs(up_dist) < 0.02:
            return 20.0
        if abs(dn_dist) < 0.02:
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
        ma20, ma60, ma120, ma200 = row["MA20"], row["MA60"], row["MA120"], row["MA200"]

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

        return bull - bear  # -100 ~ +100

    df["ma_structure_score"] = df.apply(ma_structure_score, axis=1)

    # 方向分：动量 + 回归斜率
    df["direction_score"] = (
        0.20 * df["R20"].apply(lambda x: score_neg100_100(x, -0.20, 0.20)) +
        0.30 * df["R60"].apply(lambda x: score_neg100_100(x, -0.35, 0.35)) +
        0.35 * df["R120"].apply(lambda x: score_neg100_100(x, -0.50, 0.60)) +
        0.15 * df["reg_slope"].apply(lambda x: score_neg100_100(x, -0.30, 0.30))
    )

    # 长期趋势分（0~100）
    df["trend_long_score"] = (
        0.25 * df["R20"].apply(lambda x: score_0_100(x, -0.10, 0.20)) +
        0.30 * df["R60"].apply(lambda x: score_0_100(x, -0.20, 0.35)) +
        0.30 * df["R120"].apply(lambda x: score_0_100(x, -0.30, 0.60)) +
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

    # 成交量确认分
    df["volume_score"] = df["volume_ratio"].apply(lambda x: score_0_100(x, 0.70, 1.60))

    # 位置分
    df["position_score"] = (df["channel_pos"] * 100).clip(lower=0, upper=100)

    # RS分（相对强弱）
    rs_raw = 0.4 * df["RS20"] + 0.6 * df["RS60"]
    df["rs_score"] = rs_raw.apply(lambda x: score_neg100_100(x, -0.20, 0.20))

    # breakout 分（从 -100~100 转成 0~100）
    df["breakout_score_norm"] = df["breakout_score"].apply(lambda x: (x + 100) / 2 if pd.notna(x) else np.nan)

    # 综合趋势分（0~100）
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
        0.35 * df["position_score"] +
        0.25 * df["price_ma60_dev"].apply(lambda x: score_0_100(x, 0.00, 0.20)) +
        0.20 * df["volatility_score"] +
        0.20 * df["volume_score"]
    )

    # 底部构建分
    df["base_score"] = (
        0.35 * (100 - df["position_score"]) +
        0.20 * (100 - df["volume_score"]) +
        0.20 * (100 - df["trend_stability_score"]) +
        0.15 * df["RS60"].apply(lambda x: score_0_100(x, -0.20, 0.05)) +
        0.10 * df["price_ma200_dev"].apply(lambda x: score_0_100(x, -0.35, 0.00))
    )

    return df


# =========================
# 分类逻辑
# =========================
def classify_label(last_row: pd.Series) -> str:
    needed = [
        "trend_score", "direction_score", "trend_stability_score", "adx_score",
        "volume_score", "position_score", "rs_score", "breakout_score",
        "base_score", "exhaustion_score", "ma_structure_score"
    ]
    if any(pd.isna(last_row.get(c)) for c in needed):
        return "边界模糊"

    trend_score = float(last_row["trend_score"])
    direction_score = float(last_row["direction_score"])
    trend_stability = float(last_row["trend_stability_score"])
    adx_score = float(last_row["adx_score"])
    volume_score = float(last_row["volume_score"])
    position_score = float(last_row["position_score"])
    rs_score = float(last_row["rs_score"])
    breakout_score = float(last_row["breakout_score"])
    base_score = float(last_row["base_score"])
    exhaustion_score = float(last_row["exhaustion_score"])
    ma_structure_score = float(last_row["ma_structure_score"])

    # 1. 赶顶
    if (
        position_score >= 88
        and exhaustion_score >= 72
        and trend_score >= 65
        and direction_score > 20
    ):
        return "赶顶"

    # 2. 筑底
    if (
        position_score <= 30
        and base_score >= 68
        and adx_score <= 45
        and direction_score > -45
    ):
        return "筑底"

    # 3. 上升
    if (
        trend_score >= 72
        and direction_score >= 28
        and adx_score >= 55
        and rs_score >= 15
        and (breakout_score >= 60 or ma_structure_score >= 50)
        and exhaustion_score < 78
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
# 单只股票处理
# =========================
def analyze_one_stock(stock_code: str, bench_df: pd.DataFrame):
    bs_code = to_bs_code(stock_code)

    hist = fetch_bs_data(
        bs_code,
        "date,open,high,low,close,volume",
        START_DATE,
        END_DATE
    )

    if hist.empty:
        return None, "边界模糊"

    hist = add_basic_features(hist)
    hist = add_atr(hist)
    hist = add_adx(hist)
    hist = add_donchian(hist)
    hist = add_relative_strength(hist, bench_df)
    hist = calc_scores(hist)

    if len(hist) < 220:
        return hist, "边界模糊"

    last = hist.iloc[-1]
    label = classify_label(last)
    return hist, label


# =========================
# 主程序
# =========================
def main():
    global START_DATE, END_DATE, DATE_TAG

    # 读取输入文件
    src_df = pd.read_csv(INPUT_FILE, dtype={"代码": str})
    src_df.columns = [str(c).strip() for c in src_df.columns]

    if "代码" not in src_df.columns:
        raise ValueError("输入文件必须包含【代码】列")

    original_columns = list(src_df.columns)

    # 结果容器
    result_map = {cat: [] for cat in CATEGORIES}
    total_rows = []

    # 登录 baostock
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")

    try:
        # 自动日期
        END_DATE = get_last_trading_date()
        # END_DATE = "2026-3-25"
        START_DATE = get_start_date_by_end(END_DATE, LOOKBACK_DAYS)
        DATE_TAG = END_DATE.replace("-", "")

        print(f"本次分析截止交易日：{END_DATE}")
        print(f"本次分析起始日期：{START_DATE}")

        # 先取基准指数
        print("正在获取基准指数（沪深300）...")
        bench_df = fetch_bs_data(
            BENCHMARK_CODE,
            "date,close",
            START_DATE,
            END_DATE
        )

        if bench_df.empty:
            raise RuntimeError("获取沪深300指数失败，无法计算 Relative Strength")

        total = len(src_df)

        for idx, (_, row) in enumerate(src_df.iterrows(), start=1):
            raw_code = str(row["代码"]).strip()

            try:
                hist, label = analyze_one_stock(raw_code, bench_df)

                # 原格式分类输出
                result_map[label].append(row.to_dict())

                # 总表输出：增加评分列
                summary_row = row.to_dict()
                summary_row["分类"] = label

                if hist is not None and not hist.empty:
                    last = hist.iloc[-1]
                    summary_row["截止交易日"] = END_DATE
                    summary_row["trend_score"] = round(float(last.get("trend_score", np.nan)), 2)
                    summary_row["direction_score"] = round(float(last.get("direction_score", np.nan)), 2)
                    summary_row["ma_structure_score"] = round(float(last.get("ma_structure_score", np.nan)), 2)
                    summary_row["trend_stability_score"] = round(float(last.get("trend_stability_score", np.nan)), 2)
                    summary_row["adx"] = round(float(last.get("ADX", np.nan)), 2)
                    summary_row["adx_score"] = round(float(last.get("adx_score", np.nan)), 2)
                    summary_row["breakout_score"] = round(float(last.get("breakout_score", np.nan)), 2)
                    summary_row["RS20"] = round(float(last.get("RS20", np.nan)), 4)
                    summary_row["RS60"] = round(float(last.get("RS60", np.nan)), 4)
                    summary_row["rs_score"] = round(float(last.get("rs_score", np.nan)), 2)
                    summary_row["volume_ratio"] = round(float(last.get("volume_ratio", np.nan)), 2)
                    summary_row["volume_score"] = round(float(last.get("volume_score", np.nan)), 2)
                    summary_row["position_score"] = round(float(last.get("position_score", np.nan)), 2)
                    summary_row["base_score"] = round(float(last.get("base_score", np.nan)), 2)
                    summary_row["exhaustion_score"] = round(float(last.get("exhaustion_score", np.nan)), 2)
                else:
                    summary_row["截止交易日"] = END_DATE
                    for col in [
                        "trend_score", "direction_score", "ma_structure_score",
                        "trend_stability_score", "adx", "adx_score", "breakout_score",
                        "RS20", "RS60", "rs_score", "volume_ratio", "volume_score",
                        "position_score", "base_score", "exhaustion_score"
                    ]:
                        summary_row[col] = np.nan

                total_rows.append(summary_row)

            except Exception as e:
                result_map["边界模糊"].append(row.to_dict())

                summary_row = row.to_dict()
                summary_row["截止交易日"] = END_DATE
                summary_row["分类"] = "边界模糊"
                summary_row["错误"] = str(e)
                total_rows.append(summary_row)

            if idx % 50 == 0 or idx == total:
                msg = " | ".join([f"{k}:{len(v)}" for k, v in result_map.items()])
                print(f"已处理 {idx}/{total} | {msg}")

    finally:
        bs.logout()

    # 输出分类文件（保持原格式）
    for cat in CATEGORIES:
        out_file = f"沪深_{cat}_{DATE_TAG}.csv"
        out_df = pd.DataFrame(result_map[cat], columns=original_columns)
        out_df.to_csv(out_file, index=False, encoding="utf-8-sig")
        print(f"{out_file}：{len(out_df)} 只")

    # 输出分类总表
    total_df = pd.DataFrame(total_rows)
    total_csv = f"沪深_分类总表_{DATE_TAG}.csv"
    total_xlsx = f"沪深_分类总表_{DATE_TAG}.xlsx"

    total_df.to_csv(total_csv, index=False, encoding="utf-8-sig")
    try:
        total_df.to_excel(total_xlsx, index=False)
    except Exception:
        pass

    print("\n全部完成。")
    print(f"总表已输出：{total_csv}")
    print(f"总表Excel已输出：{total_xlsx}")


if __name__ == "__main__":
    main()