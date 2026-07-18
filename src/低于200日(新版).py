# -*- coding: utf-8 -*-
import argparse
from pathlib import Path

import pandas as pd

from pipeline_config import config_value, project_path, resolve_input
from stock_utils import normalize_code_digits, write_csv


DEFAULT_OUTPUT = str(
    Path(config_value("files", "output_dir", "data/output")) / "沪深_低于200日线.csv"
)
DEFAULT_THRESHOLD = 0.0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="筛选低于 200 日均线的股票")
    parser.add_argument("--pool", help="股票池 CSV；默认使用配置中的股票池")
    parser.add_argument("--ranking", help="选股明细 Excel；默认自动选择最新文件")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="输出 CSV")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="均线偏差阈值")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    pool_file = resolve_input(args.pool, config_key="stock_pool")
    ranking_file = resolve_input(args.ranking, pattern_key="ranking_pattern")
    output_file = project_path(args.output)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    df_csv = pd.read_csv(pool_file, dtype=str)
    df_csv.columns = [str(c).strip() for c in df_csv.columns]
    original_columns = df_csv.columns.tolist()
    if "代码" not in df_csv.columns:
        raise ValueError(f"{pool_file} 必须包含【代码】列")

    df_xlsx = pd.read_excel(ranking_file)
    df_xlsx.columns = [str(c).strip() for c in df_xlsx.columns]
    if "股票代码" not in df_xlsx.columns:
        raise ValueError(f"{ranking_file} 必须包含【股票代码】列")
    if "相对200日均线偏差(%)" not in df_xlsx.columns:
        raise ValueError(f"{ranking_file} 必须包含【相对200日均线偏差(%)】列")

    df_csv["_code_norm"] = df_csv["代码"].apply(normalize_code_digits)
    df_xlsx["_code_norm"] = df_xlsx["股票代码"].apply(normalize_code_digits)
    df_xlsx["相对200日均线偏差(%)"] = pd.to_numeric(
        df_xlsx["相对200日均线偏差(%)"], errors="coerce"
    )

    below_codes = set(
        df_xlsx.loc[
            df_xlsx["相对200日均线偏差(%)"] < args.threshold,
            "_code_norm",
        ].dropna().astype(str)
    )
    result = df_csv[df_csv["_code_norm"].isin(below_codes)][original_columns].copy()
    write_csv(result, output_file)

    print(f"股票池：{pool_file}")
    print(f"选股明细：{ranking_file}")
    print(f"筛选完成，共 {len(result)} 只")
    print(f"输出文件：{output_file}")


if __name__ == "__main__":
    main()
