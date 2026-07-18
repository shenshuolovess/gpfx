# -*- coding: utf-8 -*-
"""
按配置间隔筛选 A 股股票（东方财富 Selenium 精简日志版）

功能：
1. 非交易时间不执行
2. 股票池来自统一配置的 data/input
3. 分类总表默认自动选择文件名日期最新的一份，也可通过命令行指定
4. 新浪批量抓取：代码/名称/最新价/涨幅/成交量
5. 东方财富 Selenium 抓取：量比/总市值
6. 普通弹窗自动尝试关闭
7. 遇到验证时暂停，等待手动处理后继续
8. 记录命中次数到：命中次数统计.csv
9. 命中次数文件前三列与文件二前三列保持一致
"""

import argparse
import os
import re
import time
from datetime import datetime

import pandas as pd
import requests

from pipeline_config import config_value, project_path, resolve_input
from stock_utils import (
    market_suffix,
    normalize_code_digits as normalize_code,
    read_csv_auto,
    write_csv as write_csv_utf8_sig,
)

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException, WebDriverException, NoSuchElementException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# =========================
# 配置区
# =========================
POOL_FILE = ""
CLASSIFY_FILE = ""
HIT_COUNT_FILE = str(
    project_path(config_value("files", "hit_count_file", "data/output/命中次数统计.csv"))
)

VALID_CLASS_SET = {"上升", "震荡上行", "赶顶"}

MIN_VOLUME_RATIO = 2.0
MIN_PCT = 2.0
MAX_PCT = 4.0
MAX_MARKET_CAP_YI = 2500.0

SINA_BATCH_SIZE = 200
REQUEST_TIMEOUT = 10
INTERVAL_SECONDS = int(config_value("monitor", "interval_seconds", 600))

CHROMEDRIVER_PATH = str(project_path(config_value("files", "chromedriver", "src/bin/chromedriver.exe")))
HEADLESS = bool(config_value("monitor", "headless", False))
SELENIUM_TIMEOUT = 15
PAGE_RENDER_SLEEP = 2.2

DEBUG_LOG = False

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


def code_to_sina_symbol(code6: str) -> str:
    code6 = normalize_code(code6)
    return f"{market_suffix(code6).lower()}{code6}"


def code_to_em_url(code6: str) -> str:
    code6 = normalize_code(code6)
    market = market_suffix(code6).lower()
    return f"https://quote.eastmoney.com/{market}{code6}.html"


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
def load_classify_first3_info(classify_file: str):
    """
    读取文件二前三列，后续命中次数统计文件保持前三列一致
    返回：
    - first3_cols: 文件二前三列列名
    - prefix_df: [代码6 + 前三列] 的去重结果
    """
    df = read_csv_auto(classify_file, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]

    if len(df.columns) < 3:
        raise ValueError(f"{classify_file} 至少需要有前三列")
    if "代码" not in df.columns:
        raise ValueError(f"{classify_file} 必须包含【代码】列")

    first3_cols = list(df.columns[:3])

    df["代码6"] = df["代码"].apply(normalize_code)
    prefix_df = df[["代码6"] + first3_cols].drop_duplicates(subset=["代码6"], keep="first").copy()

    return first3_cols, prefix_df


def build_prefix_map(classify_file: str):
    first3_cols, prefix_df = load_classify_first3_info(classify_file)
    prefix_map = {}

    for _, row in prefix_df.iterrows():
        code6 = normalize_code(row["代码6"])
        prefix_map[code6] = {col: row[col] for col in first3_cols}

    return first3_cols, prefix_map


# =========================
# 命中次数统计
# =========================
def load_hit_count_df() -> pd.DataFrame:
    first3_cols, _ = build_prefix_map(CLASSIFY_FILE)
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
    命中次数文件前三列与文件二前三列保持一致
    """
    first3_cols, prefix_map = build_prefix_map(CLASSIFY_FILE)

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
def load_pool_codes(pool_file: str) -> set:
    df = read_csv_auto(pool_file, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    if "代码" not in df.columns:
        raise ValueError(f"{pool_file} 中未找到【代码】列")

    df["代码6"] = df["代码"].apply(normalize_code)
    codes = set(df["代码6"].dropna().astype(str))
    codes.discard("")
    return codes


def load_classify_info(classify_file: str) -> pd.DataFrame:
    df = read_csv_auto(classify_file, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    if "代码" not in df.columns or "分类" not in df.columns:
        raise ValueError(f"{classify_file} 必须包含【代码】【分类】列")

    df["代码6"] = df["代码"].apply(normalize_code)
    df["分类"] = df["分类"].astype(str).str.strip()
    df = df[df["分类"].isin(VALID_CLASS_SET)].copy()
    return df[["代码6", "分类"]].drop_duplicates(subset=["代码6"], keep="first")


# =========================
# 新浪批量实时行情
# =========================
def fetch_sina_realtime(code_list: list[str]) -> pd.DataFrame:
    all_rows = []
    session = requests.Session()
    session.headers.update(HEADERS)

    for batch in chunk_list(code_list, SINA_BATCH_SIZE):
        symbols = ",".join(code_to_sina_symbol(c) for c in batch)
        url = f"https://hq.sinajs.cn/list={symbols}"

        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()

        text = resp.content.decode("gbk", errors="ignore")
        pattern = re.compile(r'var hq_str_(s[hz]\d{6})="([^"]*)";')
        matches = pattern.findall(text)

        for symbol, content in matches:
            parts = content.split(",")
            if len(parts) < 10:
                continue

            name = parts[0].strip()
            last_close = to_float(parts[2])
            price = to_float(parts[3])
            volume_shares = to_float(parts[8])
            amount_yuan = to_float(parts[9])

            code6 = symbol[-6:]
            pct = None
            if price is not None and last_close not in (None, 0):
                pct = (price - last_close) / last_close * 100.0

            if DEBUG_LOG:
                pct_text = f"{pct:.2f}%" if pct is not None else "None"
                print(
                    f"[新浪批量] 代码:{code6} | 名称:{name} | 最新价:{price} | 涨幅:{pct_text} | 成交量:{volume_shares}",
                    flush=True
                )

            all_rows.append({
                "代码6": code6,
                "名称": name,
                "最新价": price,
                "昨收": last_close,
                "涨幅": pct,
                "成交量": volume_shares,
                "成交额": amount_yuan,
            })

        time.sleep(0.2)

    return pd.DataFrame(all_rows)


# =========================
# Selenium 驱动
# =========================
def create_driver():
    options = Options()
    if HEADLESS:
        options.add_argument("--headless=new")

    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1600,1200")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    )

    if CHROMEDRIVER_PATH.strip():
        driver = webdriver.Chrome(service=Service(CHROMEDRIVER_PATH), options=options)
    else:
        driver = webdriver.Chrome(options=options)

    driver.set_page_load_timeout(SELENIUM_TIMEOUT)
    return driver


# =========================
# 东方财富页面抓取
# =========================
def wait_page_render(driver):
    WebDriverWait(driver, SELENIUM_TIMEOUT).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )
    time.sleep(PAGE_RENDER_SLEEP)


def extract_snippet(text: str, keyword: str, radius: int = 220):
    if not text or keyword not in text:
        return None
    idx = text.find(keyword)
    start = max(0, idx - radius)
    end = min(len(text), idx + radius)
    return text[start:end].replace("\n", " ").replace("\r", " ")


def parse_metric_from_text(text: str, label: str, need_yi: bool = False):
    if not text:
        return None

    patterns = []
    if need_yi:
        patterns.extend([
            rf"{re.escape(label)}\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)\s*亿",
            rf"{re.escape(label)}[^0-9]{{0,20}}([0-9]+(?:\.[0-9]+)?)\s*亿",
        ])
    else:
        patterns.extend([
            rf"{re.escape(label)}\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)",
            rf"{re.escape(label)}[^0-9]{{0,20}}([0-9]+(?:\.[0-9]+)?)",
        ])

    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return to_float(m.group(1))
    return None


def close_common_popups(driver):
    xpaths = [
        "//div[contains(@class,'close')]",
        "//span[contains(@class,'close')]",
        "//i[contains(@class,'close')]",
        "//button[contains(@class,'close')]",
        "//*[text()='关闭']",
        "//*[text()='我知道了']",
        "//*[text()='知道了']",
        "//*[text()='取消']",
    ]

    count = 0
    for xp in xpaths:
        try:
            elems = driver.find_elements(By.XPATH, xp)
            for e in elems[:5]:
                if e.is_displayed():
                    try:
                        e.click()
                        count += 1
                        time.sleep(0.2)
                    except Exception:
                        pass
        except Exception:
            pass

    if DEBUG_LOG and count > 0:
        print(f"[{now_str()}] 已尝试关闭普通弹窗 {count} 次", flush=True)


def page_has_verification(text: str) -> bool:
    if not text:
        return False
    keywords = [
        "安全验证", "行为验证", "拖动滑块", "滑块", "拼图", "验证码",
        "请完成安全验证", "请按住滑块", "验证后继续"
    ]
    return any(k in text for k in keywords)


def ensure_no_verification(driver, code6: str):
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        body_text = ""

    if page_has_verification(body_text):
        print(f"[{now_str()}] 代码:{code6} 页面出现验证，请手动处理后按回车继续。", flush=True)
        input()
        time.sleep(1.0)


def fetch_eastmoney_metrics_selenium(driver, code6: str):
    url = code_to_em_url(code6)

    for attempt in range(1, 4):
        try:
            driver.get(url)
            wait_page_render(driver)

            close_common_popups(driver)
            ensure_no_verification(driver, code6)

            body_text = driver.find_element(By.TAG_NAME, "body").text
            title = driver.title

            market_block = extract_snippet(body_text, "总市值", 260)
            market_cap_yi = None
            volume_ratio = None

            if market_block:
                market_cap_yi = parse_metric_from_text(market_block, "总市值", need_yi=True)
                volume_ratio = parse_metric_from_text(market_block, "量比", need_yi=False)

            if volume_ratio is None:
                volume_ratio = parse_metric_from_text(body_text, "量比", need_yi=False)

            if volume_ratio is None and market_cap_yi is None and attempt < 3:
                print(f"[东财] 代码:{code6} | 第{attempt}次未抓到量比和总市值，准备重试", flush=True)
                time.sleep(1.5)
                continue

            vr_text = f"{volume_ratio:.2f}" if volume_ratio is not None else "None"
            cap_text = f"{market_cap_yi:.2f}亿" if market_cap_yi is not None else "None"

            print(
                f"[东财] 代码:{code6} | 量比:{vr_text} | 总市值:{cap_text}",
                flush=True
            )

            if DEBUG_LOG and market_block:
                print(f"[东财片段] 代码:{code6} | 标题:{title} | 基础行情块: {market_block}", flush=True)

            return {
                "代码6": code6,
                "量比": volume_ratio,
                "总市值亿": market_cap_yi,
                "来源URL": url,
            }

        except (TimeoutException, WebDriverException, NoSuchElementException) as e:
            if attempt < 3:
                print(f"[东财] 代码:{code6} | 第{attempt}次抓取异常:{e} | 准备重试", flush=True)
                time.sleep(1.5)
                continue
            print(f"[东财] 代码:{code6} | 抓取异常:{e}", flush=True)

    return {
        "代码6": code6,
        "量比": None,
        "总市值亿": None,
        "来源URL": url,
    }


# =========================
# 候选集与实时表
# =========================
def build_candidate_codes(pool_codes: set, classify_df: pd.DataFrame) -> list[str]:
    return sorted(set(pool_codes) & set(classify_df["代码6"]))


def build_realtime_table(candidate_codes: list[str], classify_df: pd.DataFrame, driver) -> pd.DataFrame:
    sina_df = fetch_sina_realtime(candidate_codes)
    if sina_df.empty:
        return sina_df

    sina_df = sina_df.merge(classify_df, on="代码6", how="inner")

    pre_df = sina_df[
        (sina_df["涨幅"].notna()) &
        (sina_df["涨幅"] >= MIN_PCT) &
        (sina_df["涨幅"] <= MAX_PCT)
    ].copy()

    if pre_df.empty:
        return pre_df

    pre_codes = pre_df["代码6"].drop_duplicates().tolist()
    total = len(pre_codes)
    rows = []

    for idx, code6 in enumerate(pre_codes, start=1):
        print(f"[{now_str()}] 抓取东方财富字段 {idx}/{total}: {code6}", flush=True)
        rows.append(fetch_eastmoney_metrics_selenium(driver, code6))
        time.sleep(0.5)

    em_df = pd.DataFrame(rows)
    out = pre_df.merge(em_df, on="代码6", how="left")
    return out


def filter_stocks(rt_df: pd.DataFrame) -> pd.DataFrame:
    if rt_df.empty:
        return rt_df

    df = rt_df[
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


# =========================
# 单次执行
# =========================
def run_once(driver):
    if not is_trading_time():
        print(f"[{now_str()}] 当前非交易时间，跳过本轮。", flush=True)
        print("-" * 100, flush=True)
        return

    pool_codes = load_pool_codes(POOL_FILE)
    classify_df = load_classify_info(CLASSIFY_FILE)
    candidate_codes = build_candidate_codes(pool_codes, classify_df)

    print(f"[{now_str()}] 股票池数量: {len(pool_codes)}", flush=True)
    print(f"[{now_str()}] 分类命中数量: {len(classify_df)}", flush=True)
    print(f"[{now_str()}] 候选股票数量: {len(candidate_codes)}", flush=True)

    if not candidate_codes:
        print(f"[{now_str()}] 候选股票为空", flush=True)
        print("-" * 100, flush=True)
        return

    rt_df = build_realtime_table(candidate_codes, classify_df, driver)
    if rt_df.empty:
        print(f"[{now_str()}] 初筛后无股票命中涨幅区间", flush=True)
        print("-" * 100, flush=True)
        return

    result_df = filter_stocks(rt_df)
    update_hit_count(result_df)
    result_df = attach_hit_count(result_df)

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
    parser.add_argument("--classification", help="分类总表；默认自动选择最新文件")
    parser.add_argument("--interval", type=int, default=INTERVAL_SECONDS, help="轮询间隔秒数")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=HEADLESS)
    parser.add_argument("--once", action="store_true", help="只执行一轮后退出")
    return parser.parse_args(argv)


def main(argv=None):
    global POOL_FILE, CLASSIFY_FILE, INTERVAL_SECONDS, HEADLESS

    args = parse_args(argv)
    POOL_FILE = str(resolve_input(args.pool, config_key="stock_pool"))
    CLASSIFY_FILE = str(resolve_input(args.classification, pattern_key="classification_pattern"))
    INTERVAL_SECONDS = max(1, args.interval)
    HEADLESS = args.headless

    print(f"股票池：{POOL_FILE}", flush=True)
    print(f"分类总表：{CLASSIFY_FILE}", flush=True)
    print("启动 Selenium 浏览器...", flush=True)
    driver = create_driver()

    try:
        print(f"开始监控，轮询间隔 {format_interval(INTERVAL_SECONDS)}...\n", flush=True)
        while True:
            try:
                run_once(driver)
            except Exception as e:
                print(f"[{now_str()}] 执行异常: {e}", flush=True)
                print("-" * 100, flush=True)

            if args.once:
                break
            time.sleep(INTERVAL_SECONDS)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
