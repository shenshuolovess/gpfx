# -*- coding: utf-8 -*-
import argparse
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from logging_utils import default_log_dir, get_rotating_logger
from pipeline_config import config_value, project_path, resolve_input
from stock_utils import (
    dated_output_path,
    normalize_code_suffix,
    read_table,
    require_columns,
    write_csv,
)

# 原来是单一分类：TARGET_CLASS = "震荡上行"
# 现在扩展为三种分类
TARGET_CLASSES = ["震荡上行", "上升", "赶顶"]

MA_COL = "相对200日均线偏差(%)"
MA_MIN = 0
MA_MAX = 0.015

OUTPUT_PREFIX = "震荡上行_上升_赶顶_200日均线附近"
LOG_PREFIX = "筛选日志_200日均线"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="筛选强趋势且位于 200 日均线上方附近的股票")
    parser.add_argument("--classification", help="分类总表；默认自动选择最新文件")
    parser.add_argument("--ranking", help="选股明细 Excel；默认自动选择最新文件")
    parser.add_argument("--pool", help="输出模板股票池；默认使用配置中的股票池")
    parser.add_argument(
        "--output-dir",
        default=config_value("files", "output_dir", "data/output"),
        help="输出目录",
    )
    parser.add_argument("--log-dir", default=default_log_dir(), help="轮转日志目录")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    base_dir = Path(__file__).resolve().parent
    output_dir = project_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir = project_path(args.log_dir)
    file1_path = resolve_input(args.classification, pattern_key="classification_pattern")
    file2_path = resolve_input(args.ranking, pattern_key="ranking_pattern")
    file3_path = resolve_input(args.pool, config_key="stock_pool")
    today = datetime.now().strftime("%Y%m%d")

    log_file = dated_output_path(log_dir, LOG_PREFIX, date_tag=today, suffix=".log")
    output_file = dated_output_path(output_dir, OUTPUT_PREFIX, date_tag=today)

    logger = get_rotating_logger("stock_filter_ma200", log_file)

    logger.info("========== 开始执行股票筛选 ==========")
    logger.info(f"工作目录：{base_dir}")
    logger.info(f"文件1-分类总表：{file1_path}")
    logger.info(f"文件2-top200明细：{file2_path}")
    logger.info(f"文件3-输出模板：{file3_path}")
    logger.info(f"筛选条件1：文件1 分类 in {TARGET_CLASSES}")
    logger.info(f"筛选条件2：文件2 {MA_COL} 在 [{MA_MIN}, {MA_MAX}] 之间，包含边界")
    logger.info(f"输出文件：{output_file.name}")

    df1 = read_table(file1_path)
    df2 = read_table(file2_path)
    df3 = read_table(file3_path)

    logger.info(f"文件1读取完成：{len(df1)} 行，{len(df1.columns)} 列")
    logger.info(f"文件2读取完成：{len(df2)} 行，{len(df2.columns)} 列")
    logger.info(f"文件3模板读取完成：{len(df3)} 行，{len(df3.columns)} 列")
    logger.info(f"文件1列名：{list(df1.columns)}")
    logger.info(f"文件2列名：{list(df2.columns)}")
    logger.info(f"文件3模板列名：{list(df3.columns)}")

    require_columns(df1, ["代码", "名称", "市场", "分类"], "文件1")
    require_columns(df2, ["股票代码", "名称", MA_COL], "文件2")
    require_columns(df3, ["代码", "名称", "市场"], "文件3模板")

    df1 = df1.copy()
    df2 = df2.copy()
    df3 = df3.copy()

    df1["_标准代码"] = df1["代码"].apply(normalize_code_suffix)
    df2["_标准代码"] = df2["股票代码"].apply(normalize_code_suffix)
    df3["_标准代码"] = df3["代码"].apply(normalize_code_suffix)

    bad1 = df1[df1["_标准代码"].eq("")]
    bad2 = df2[df2["_标准代码"].eq("")]
    bad3 = df3[df3["_标准代码"].eq("")]
    if len(bad1) > 0:
        logger.warning(f"文件1存在无法识别代码：{len(bad1)} 行，将自动忽略")
    if len(bad2) > 0:
        logger.warning(f"文件2存在无法识别代码：{len(bad2)} 行，将自动忽略")
    if len(bad3) > 0:
        logger.warning(f"文件3模板存在无法识别代码：{len(bad3)} 行，将自动忽略")

    df1["_分类清洗"] = df1["分类"].astype(str).str.strip()

    # 原逻辑：
    # step1 = df1[df1["_分类清洗"].eq(TARGET_CLASS)].copy()
    # 新逻辑：分类属于 震荡上行 / 上升 / 赶顶 三者之一
    step1 = df1[df1["_分类清洗"].isin(TARGET_CLASSES)].copy()

    logger.info(f"步骤1完成：文件1中分类属于【{TARGET_CLASSES}】的股票数量：{len(step1)}")
    logger.info(f"步骤1去重代码数：{step1['_标准代码'].nunique()}")

    # 打印三类分别命中多少，方便核对
    class_counts = step1["_分类清洗"].value_counts().to_dict()
    logger.info(f"步骤1分类分布：{class_counts}")

    df2[MA_COL] = pd.to_numeric(df2[MA_COL], errors="coerce")
    ma_missing = df2[MA_COL].isna().sum()
    if ma_missing > 0:
        logger.warning(f"文件2中【{MA_COL}】无法转为数值或为空：{ma_missing} 行，这些行不会通过均线偏差筛选")

    step1_codes = set(step1["_标准代码"])
    df2_in_step1 = df2[df2["_标准代码"].isin(step1_codes)].copy()
    logger.info(f"步骤2-前置匹配：步骤1股票在文件2中命中的数量：{len(df2_in_step1)}，去重代码数：{df2_in_step1['_标准代码'].nunique()}")

    step2 = df2_in_step1[
        df2_in_step1[MA_COL].between(MA_MIN, MA_MAX, inclusive="both")
    ].copy()

    logger.info(f"步骤2完成：同时满足200日均线偏差区间的股票数量：{len(step2)}，去重代码数：{step2['_标准代码'].nunique()}")

    if len(step2) > 0:
        logger.info("步骤2结果前20行：")
        for _, row in step2.head(20).iterrows():
            logger.info(f"  {row.get('股票代码', '')} {row.get('名称', '')} {MA_COL}={row.get(MA_COL, '')}")

    passed_codes = set(step2["_标准代码"])

    template_cols = list(df3.columns)
    template_cols_no_helper = [c for c in template_cols if c != "_标准代码"]

    out = df3[df3["_标准代码"].isin(passed_codes)].copy()
    logger.info(f"步骤3：按文件3模板匹配输出，命中模板股票数量：{len(out)}，去重代码数：{out['_标准代码'].nunique()}")

    missed_in_template = sorted(passed_codes - set(out["_标准代码"]))
    if missed_in_template:
        logger.warning(f"通过步骤2但未在文件3模板中找到的代码数量：{len(missed_in_template)}")
        logger.warning(f"未命中模板代码示例：{missed_in_template[:30]}")

    out = out[template_cols_no_helper].drop_duplicates(subset=["代码"], keep="first")

    logger.info(f"最终输出数量：{len(out)}")
    if len(out) > 0:
        logger.info("最终输出前20行：")
        for _, row in out.head(20).iterrows():
            logger.info(f"  {row.get('代码', '')} {row.get('名称', '')} {row.get('市场', '')}")
    else:
        logger.warning("最终结果为空，请检查：1）分类名称是否完全为震荡上行/上升/赶顶；2）文件2代码格式是否一致；3）200日均线偏差是否为小数口径")

    write_csv(out, output_file)

    logger.info(f"CSV输出完成：{output_file}")
    logger.info(f"日志输出完成：{log_file}")
    logger.info("========== 执行结束 ==========")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.exception(f"程序执行失败：{e}")
        raise
