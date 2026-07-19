"""从最新分类总表生成透明机会评分榜单。"""

from __future__ import annotations

import argparse

import pandas as pd

from opportunity_score import (
    add_opportunity_scores, load_opportunity_config, opportunity_output,
)
from pipeline_config import config_value, project_path, resolve_input
from stock_utils import date_tag_from_path, dated_output_path, normalize_code, read_csv_auto, write_csv


def benchmark_metrics(frame: pd.DataFrame) -> dict:
    if "代码" in frame.columns:
        codes = frame["代码"].map(lambda value: normalize_code(value, "suffix"))
        matched = frame[codes.eq("000300.SH")]
        if not matched.empty:
            return matched.iloc[-1].to_dict()
    if "名称" in frame.columns:
        matched = frame[frame["名称"].astype(str).eq("沪深300")]
        if not matched.empty:
            return matched.iloc[-1].to_dict()
    return {}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="生成与九种分类解耦的机会评分")
    parser.add_argument("--input", help="分类总表；默认自动选择日期最新文件")
    parser.add_argument("--output-dir", default=config_value("files", "output_dir", "data/output"))
    parser.add_argument("--config", default="opportunity_score.toml")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    source = resolve_input(args.input, pattern_key="classification_pattern")
    frame = read_csv_auto(source)
    required = {"代码", "名称", "trend_score", "direction_score", "rs_score"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise KeyError(f"分类总表缺少机会评分字段：{missing}")
    scored = add_opportunity_scores(
        frame, market_metrics=benchmark_metrics(frame),
        config=load_opportunity_config(args.config),
    )
    result = opportunity_output(scored)
    output_dir = project_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    date_tag = date_tag_from_path(source)
    output_file = dated_output_path(output_dir, "沪深_机会评分", date_tag=date_tag)
    write_csv(result, output_file)
    print(f"输入文件：{source}")
    print(f"大盘环境：{'沪深300' if benchmark_metrics(frame) else '缺失，调整为0'}")
    print(f"评分完成：{len(result)} 只股票")
    print(f"输出文件：{output_file}")
    if not result.empty:
        print("\n机会评分前10")
        print(result[["代码", "名称", "分类", "机会评分", "机会等级"]].head(10).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
