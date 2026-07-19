"""对机会评分做按截面分层回测，检验排序能力而非分类收益。"""

from __future__ import annotations

import argparse
from datetime import datetime

import numpy as np
import pandas as pd

from backtest_classification import load_rating_module, parse_horizons, prepare_scored_history
from classification_rules import CURRENT_RULES
from compare_classification_rules import build_comparison_samples
from history_store import load_history
from opportunity_score import load_opportunity_config, score_opportunity
from pipeline_config import config_value, project_path
from stock_utils import timestamped_output_path, write_csv


BUCKETS = ["Q1偏低", "Q2", "Q3", "Q4", "Q5偏高"]


def benchmark_metrics_by_snapshot(args, snapshot_dates: list[str]) -> dict[str, dict]:
    rating = load_rating_module()
    benchmark = load_history(
        project_path(args.history_dir), rating.BENCHMARK_CODE, kind="benchmark",
        verify_checksum=not args.no_verify_history,
    )
    if benchmark is None or benchmark.empty:
        return {}
    scored = prepare_scored_history(rating, benchmark, benchmark).sort_values("date")
    result = {}
    for snapshot in snapshot_dates:
        eligible = scored[scored["date"].astype(str) <= snapshot]
        if not eligible.empty:
            result[snapshot] = eligible.iloc[-1].to_dict()
    return result


def add_scores_and_buckets(
    detail: pd.DataFrame, market_by_date: dict[str, dict], config,
) -> pd.DataFrame:
    result = detail.copy()
    scores = result.apply(
        lambda row: score_opportunity(
            row, market_metrics=market_by_date.get(row["回测截面日"]), config=config,
        ), axis=1,
    )
    result["机会评分"] = scores.map(lambda value: value["机会评分"])
    result["机会等级"] = scores.map(lambda value: value["机会等级"])
    result["风险扣分"] = scores.map(lambda value: value["风险扣分"])
    result["大盘调整"] = scores.map(lambda value: value["大盘调整"])
    result["机会评分说明"] = scores.map(lambda value: value["机会评分说明"])
    result["机会分层"] = "数据不足"
    for _, indices in result.groupby("回测截面日").groups.items():
        values = pd.to_numeric(result.loc[indices, "机会评分"], errors="coerce")
        valid = values.dropna()
        if len(valid) < 5:
            continue
        percentile = valid.rank(method="first", pct=True)
        result.loc[valid.index, "机会分层"] = pd.cut(
            percentile, bins=[0, .2, .4, .6, .8, 1], labels=BUCKETS,
            include_lowest=True,
        ).astype(str)
    return result


def summarize_buckets(detail: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    rows = []
    periods = [("总体", detail)] + list(detail.groupby("样本区间", sort=False))
    for period_name, period in periods:
        for horizon in horizons:
            column = f"未来{horizon}日同池超额"
            for bucket in BUCKETS:
                subset = period[period["机会分层"] == bucket]
                values = pd.to_numeric(subset[column], errors="coerce").dropna()
                rows.append({
                    "样本区间": period_name, "周期": f"{horizon}日", "机会分层": bucket,
                    "样本数": len(values), "不同股票数": subset.loc[values.index, "代码"].nunique(),
                    "覆盖截面数": subset.loc[values.index, "回测截面日"].nunique(),
                    "平均机会评分": pd.to_numeric(subset.loc[values.index, "机会评分"], errors="coerce").mean(),
                    "平均同池超额": values.mean() if len(values) else np.nan,
                    "中位同池超额": values.median() if len(values) else np.nan,
                    "跑赢同池率": (values > 0).mean() if len(values) else np.nan,
                })
    return pd.DataFrame(rows)


def summarize_ranking_quality(detail: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    snapshot_rows = []
    for date, snapshot in detail.groupby("回测截面日"):
        for horizon in horizons:
            column = f"未来{horizon}日同池超额"
            valid = snapshot[["机会评分", "机会分层", column]].dropna()
            ic = valid["机会评分"].corr(valid[column], method="spearman") if len(valid) >= 5 else np.nan
            top = pd.to_numeric(valid.loc[valid["机会分层"] == "Q5偏高", column], errors="coerce").mean()
            bottom = pd.to_numeric(valid.loc[valid["机会分层"] == "Q1偏低", column], errors="coerce").mean()
            snapshot_rows.append({
                "回测截面日": date, "样本区间": snapshot["样本区间"].iloc[0],
                "周期": f"{horizon}日", "秩相关IC": ic,
                "Q5减Q1同池超额": top - bottom if pd.notna(top) and pd.notna(bottom) else np.nan,
            })
    snapshots = pd.DataFrame(snapshot_rows)
    rows = []
    periods = [("总体", snapshots)] + list(snapshots.groupby("样本区间", sort=False))
    for period_name, period in periods:
        for horizon in horizons:
            subset = period[period["周期"] == f"{horizon}日"]
            ic = pd.to_numeric(subset["秩相关IC"], errors="coerce").dropna()
            spread = pd.to_numeric(subset["Q5减Q1同池超额"], errors="coerce").dropna()
            rows.append({
                "样本区间": period_name, "周期": f"{horizon}日",
                "覆盖截面数": max(len(ic), len(spread)),
                "平均秩相关IC": ic.mean() if len(ic) else np.nan,
                "IC为正截面比例": (ic > 0).mean() if len(ic) else np.nan,
                "平均Q5减Q1同池超额": spread.mean() if len(spread) else np.nan,
                "Q5跑赢Q1截面比例": (spread > 0).mean() if len(spread) else np.nan,
                "结论": "排序方向稳定" if len(ic) >= 10 and ic.mean() > 0 and (ic > 0).mean() >= .6 else "尚未证明稳定排序能力",
            })
    return pd.DataFrame(rows)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="机会评分同池分层历史回测")
    parser.add_argument("--pool")
    parser.add_argument("--history-dir", default=config_value("files", "history_dir", "data/history"))
    parser.add_argument("--output-dir", default=config_value("files", "output_dir", "data/output"))
    parser.add_argument("--config", default="opportunity_score.toml")
    parser.add_argument("--max-stocks", type=int, default=0)
    parser.add_argument("--snapshots", type=int, default=30)
    parser.add_argument("--step", type=int, default=5)
    parser.add_argument("--horizons", type=parse_horizons, default=parse_horizons("5"))
    parser.add_argument("--no-verify-history", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.step <= 0:
        raise ValueError("step必须为正数")
    detail, stats, dates = build_comparison_samples(args, {"baseline": CURRENT_RULES})
    config = load_opportunity_config(args.config)
    market = benchmark_metrics_by_snapshot(args, dates)
    scored = add_scores_and_buckets(detail, market, config)
    buckets = summarize_buckets(scored, args.horizons)
    quality = summarize_ranking_quality(scored, args.horizons)
    output_dir = project_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outputs = {
        "明细": timestamped_output_path(output_dir, "机会评分回测_明细", timestamp=timestamp, suffix=".csv"),
        "分层": timestamped_output_path(output_dir, "机会评分回测_分层", timestamp=timestamp, suffix=".csv"),
        "排序质量": timestamped_output_path(output_dir, "机会评分回测_排序质量", timestamp=timestamp, suffix=".csv"),
    }
    for name, frame in (("明细", scored), ("分层", buckets), ("排序质量", quality)):
        write_csv(frame, outputs[name])
    print("\n机会评分回测完成")
    print(f"样本：{len(scored)} | 截面：{len(dates)} | 缺历史：{stats['missing_history']}")
    for name, path in outputs.items():
        print(f"{name}：{path}")
    print("\n排序质量")
    print(quality.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
