# -*- coding: utf-8 -*-
import argparse
from datetime import datetime

import pandas as pd

from pipeline_config import config_value, project_path, resolve_input
from stock_utils import (
    date_tag_from_path, dated_output_path, normalize_code_suffix,
    read_csv_auto, write_csv,
)

TOL = 1e-9                       # 浮点比较容差


# =========================
# 工具函数
# =========================
def market_from_code(code: str) -> str:
    if code.endswith(".SH"):
        return "上海"
    if code.endswith(".SZ"):
        return "深圳"
    if code.endswith(".BJ"):
        return "北京"
    return ""


def ensure_numeric(df: pd.DataFrame, cols):
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")


def eq_zero(series: pd.Series, tol: float = TOL) -> pd.Series:
    """
    判断是否等于0，考虑浮点误差
    """
    return series.fillna(999999).abs() <= tol


def build_output(df_src: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame()
    out["代码"] = df_src["股票代码"].apply(normalize_code_suffix)
    out["名称"] = df_src["名称"].astype(str)
    out["市场"] = out["代码"].apply(market_from_code)

    out = out.drop_duplicates(subset=["代码"], keep="first")
    out = out[["代码", "名称", "市场"]].copy()
    return out


def update_count_history(path, date_tag: str, counts: dict[str, int]) -> pd.DataFrame:
    history_path = project_path(path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    columns = ["日期", "强势数量", "近期新高数量", "历史新高数量"]
    if history_path.exists():
        history = read_csv_auto(history_path, dtype={"日期": str})
        history = history.reindex(columns=columns)
    else:
        history = pd.DataFrame(columns=columns)
    try:
        display_date = datetime.strptime(date_tag, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        display_date = str(date_tag)
    row = pd.DataFrame([{
        "日期": display_date,
        "强势数量": int(counts["强势"]),
        "近期新高数量": int(counts["近期新高"]),
        "历史新高数量": int(counts["历史新高"]),
    }])
    history = history.loc[history["日期"].astype(str) != display_date]
    history = pd.concat([history, row], ignore_index=True).sort_values("日期")
    write_csv(history, history_path)
    return history


# =========================
# 主逻辑
# =========================
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="从最新选股明细计算强势和新高标的")
    parser.add_argument("--input", help="选股明细 Excel；默认自动选择最新文件")
    parser.add_argument(
        "--output-dir",
        default=config_value("files", "target_output_dir", "计算标的"),
        help="输出目录",
    )
    parser.add_argument(
        "--count-history",
        default=config_value("files", "target_count_history", "data/output/计算标的数量历史.csv"),
        help="三类标的每日数量历史CSV",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    input_file = resolve_input(args.input, pattern_key="ranking_pattern")
    output_dir = project_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"读取文件: {input_file}")

    df = pd.read_excel(input_file)

    required_cols = [
        "股票代码",
        "名称",
        "当前价格",
        "总市值(亿)",
        "最近15个交易日涨幅",
        "最近15个交易日单日涨幅的交易日个数",
        "上一个交易日交易量相对之前15日均量(%)",
        "相对50日均线偏差(%)",
        "相对150日均线偏差(%)",
        "相对200日均线偏差(%)",
        "50日均线相对150日均线偏差(%)",
        "50日均线相对200日均线偏差(%)",
        "150日均线相对200日均线偏差(%)",
        "相对近200日低点偏差(%)",
        "相对近200日高点偏差(%)",
        "RS排名",
        "相对历史高点跌幅",
    ]

    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError("输入文件缺少以下必要字段：\n" + "\n".join(missing))

    numeric_cols = [
        "当前价格",
        "总市值(亿)",
        "最近15个交易日涨幅",
        "最近15个交易日单日涨幅的交易日个数",
        "上一个交易日交易量相对之前15日均量(%)",
        "相对50日均线偏差(%)",
        "相对150日均线偏差(%)",
        "相对200日均线偏差(%)",
        "50日均线相对150日均线偏差(%)",
        "50日均线相对200日均线偏差(%)",
        "150日均线相对200日均线偏差(%)",
        "相对近200日低点偏差(%)",
        "相对近200日高点偏差(%)",
        "RS排名",
        "相对历史高点跌幅",
    ]
    ensure_numeric(df, numeric_cols)

    # -------------------------
    # 文件一：强势
    # 1、当前价格 > 5
    # 2、总市值(亿) > 100
    # 3、最近15个交易日涨幅 > 0.2
    # 4、最近15个交易日单日涨幅的交易日个数 > 10
    # 5、上一个交易日交易量相对之前15日均量(%) > 0.25
    # -------------------------
    cond1 = (
        (df["当前价格"] > 5) &
        (df["总市值(亿)"] > 100) &
        (df["最近15个交易日涨幅"] > 0.2) &
        (df["最近15个交易日单日涨幅的交易日个数"] > 10) &
        (df["上一个交易日交易量相对之前15日均量(%)"] > 0.25)
    )
    df_1 = df[cond1].copy()

    # -------------------------
    # 文件二：近期新高
    # 1、当前价格 > 5
    # 2、总市值(亿) > 100
    # 3、六个均线关系全部 > 0
    # 4、相对近200日低点偏差(%) > 0.25
    # 5、相对近200日高点偏差(%) == 0
    # 6、RS排名 > 70
    # -------------------------
    cond2 = (
        (df["当前价格"] > 5) &
        (df["总市值(亿)"] > 100) &
        (df["相对50日均线偏差(%)"] > 0) &
        (df["相对150日均线偏差(%)"] > 0) &
        (df["相对200日均线偏差(%)"] > 0) &
        (df["50日均线相对150日均线偏差(%)"] > 0) &
        (df["50日均线相对200日均线偏差(%)"] > 0) &
        (df["150日均线相对200日均线偏差(%)"] > 0) &
        (df["相对近200日低点偏差(%)"] > 0.25) &
        eq_zero(df["相对近200日高点偏差(%)"]) &
        (df["RS排名"] > 70)
    )
    df_2 = df[cond2].copy()

    # -------------------------
    # 文件三：历史新高
    # 在文件二基础上增加：
    # 相对历史高点跌幅 == 0
    # -------------------------
    cond3 = cond2 & eq_zero(df["相对历史高点跌幅"])
    df_3 = df[cond3].copy()

    # 构造输出
    out_1 = build_output(df_1)
    out_2 = build_output(df_2)
    out_3 = build_output(df_3)

    # 日期后缀
    date_str = date_tag_from_path(input_file, datetime.now().strftime("%Y%m%d"))

    file_1 = dated_output_path(output_dir, "强势", date_tag=date_str)
    file_2 = dated_output_path(output_dir, "近期新高", date_tag=date_str)
    file_3 = dated_output_path(output_dir, "历史新高", date_tag=date_str)

    write_csv(out_1, file_1)
    write_csv(out_2, file_2)
    write_csv(out_3, file_3)

    history = update_count_history(
        args.count_history,
        date_str,
        {"强势": len(out_1), "近期新高": len(out_2), "历史新高": len(out_3)},
    )

    print("输出完成：")
    print(f"强势: {file_1}，共 {len(out_1)} 条")
    print(f"近期新高: {file_2}，共 {len(out_2)} 条")
    print(f"历史新高: {file_3}，共 {len(out_3)} 条")
    print(f"数量历史: {project_path(args.count_history)}，共 {len(history)} 个日期")


if __name__ == "__main__":
    main()
