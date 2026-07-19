"""诊断机会因子，并以带禁运期的月度滚动方式验证极简候选模型。"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from backtest_classification import parse_horizons
from pipeline_config import config_value, project_path
from stock_utils import latest_matching_file, read_csv_auto, timestamped_output_path, write_csv


MODEL_DEFINITIONS = {
    "趋势延续": (("trend_score", 1), ("rs_score", 1), ("breakout_score", 1)),
    "回撤反弹": (("rs_score", -1), ("position_score", -1), ("base_score", 1)),
}
MODEL_DESCRIPTIONS = {
    "当前机会评分": "现有v1综合评分，仅作为对照，不参与滚动选择",
    "趋势延续": "趋势得分、相对强弱、突破得分的截面等权排名",
    "回撤反弹": "相对弱势、低位置、筑底得分的截面等权排名",
}


def add_minimal_model_scores(detail: pd.DataFrame) -> pd.DataFrame:
    """以每个历史截面的百分位生成0—100分，避免不同指标量纲干扰。"""
    required = {"回测截面日"}
    required.update(field for fields in MODEL_DEFINITIONS.values() for field, _ in fields)
    missing = sorted(required - set(detail.columns))
    if missing:
        raise ValueError(f"回测明细缺少字段：{', '.join(missing)}")

    result = detail.copy()
    for model, fields in MODEL_DEFINITIONS.items():
        parts = []
        for field, direction in fields:
            numeric = pd.to_numeric(result[field], errors="coerce")
            rank = numeric.groupby(result["回测截面日"]).rank(method="average", pct=True)
            parts.append(rank if direction > 0 else 1 - rank)
        matrix = pd.concat(parts, axis=1)
        available = matrix.notna().sum(axis=1)
        result[model] = matrix.mean(axis=1).where(available >= 2).mul(100)
    return result


def _snapshot_metric(snapshot: pd.DataFrame, score_column: str, return_column: str) -> tuple[float, float]:
    valid = snapshot[[score_column, return_column]].apply(pd.to_numeric, errors="coerce").dropna()
    if len(valid) < 20 or valid[score_column].nunique() < 5 or valid[return_column].nunique() < 2:
        return np.nan, np.nan
    ic = valid[score_column].corr(valid[return_column], method="spearman")
    percentile = valid[score_column].rank(method="first", pct=True)
    top = valid.loc[percentile > .8, return_column].mean()
    bottom = valid.loc[percentile <= .2, return_column].mean()
    return ic, top - bottom


def build_snapshot_metrics(detail: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    model_columns = [model for model in MODEL_DEFINITIONS if model in detail.columns]
    if "机会评分" in detail.columns:
        model_columns.insert(0, "当前机会评分")
        detail = detail.rename(columns={"机会评分": "当前机会评分"})
    rows = []
    for date, snapshot in detail.groupby("回测截面日", sort=True):
        sample_period = snapshot["样本区间"].iloc[0] if "样本区间" in snapshot else "未划分"
        for horizon in horizons:
            return_column = f"未来{horizon}日同池超额"
            if return_column not in snapshot:
                raise ValueError(f"回测明细不包含{horizon}日收益，请先用该周期运行机会评分回测")
            for model in model_columns:
                ic, spread = _snapshot_metric(snapshot, model, return_column)
                rows.append({
                    "回测截面日": str(date), "月份": str(date)[:7], "样本区间": sample_period,
                    "周期": f"{horizon}日", "模型": model, "秩相关IC": ic,
                    "Q5减Q1同池超额": spread, "截面股票数": len(snapshot),
                })
    return pd.DataFrame(rows)


def summarize_diagnostics(metrics: pd.DataFrame) -> pd.DataFrame:
    rows = []
    periods = [("总体", metrics)] + list(metrics.groupby("样本区间", sort=False))
    for period_name, period in periods:
        for (horizon, model), group in period.groupby(["周期", "模型"], sort=False):
            ic = pd.to_numeric(group["秩相关IC"], errors="coerce").dropna()
            spread = pd.to_numeric(group["Q5减Q1同池超额"], errors="coerce").dropna()
            rows.append({
                "样本区间": period_name, "周期": horizon, "模型": model,
                "模型定义": MODEL_DESCRIPTIONS.get(model, ""),
                "覆盖截面数": max(len(ic), len(spread)),
                "平均秩相关IC": ic.mean() if len(ic) else np.nan,
                "IC为正截面比例": (ic > 0).mean() if len(ic) else np.nan,
                "平均Q5减Q1同池超额": spread.mean() if len(spread) else np.nan,
                "Q5跑赢Q1截面比例": (spread > 0).mean() if len(spread) else np.nan,
            })
    return pd.DataFrame(rows)


def rolling_monthly_validation(
    metrics: pd.DataFrame,
    *,
    train_months: int = 12,
    min_train_snapshots: int = 24,
    min_positive_ratio: float = .55,
) -> pd.DataFrame:
    """月初只使用当时已知结果选模型；禁运期按2倍预测周期的自然日保守估算。"""
    candidate_models = list(MODEL_DEFINITIONS)
    work = metrics[metrics["模型"].isin(candidate_models)].copy()
    work["日期值"] = pd.to_datetime(work["回测截面日"])
    rows = []
    for horizon_text, horizon_data in work.groupby("周期", sort=False):
        horizon = int(str(horizon_text).removesuffix("日"))
        for month, evaluation in horizon_data.groupby("月份", sort=True):
            evaluation_start = evaluation["日期值"].min()
            cutoff = evaluation_start - pd.Timedelta(days=max(7, horizon * 2))
            training_start = cutoff - pd.DateOffset(months=train_months)
            training = horizon_data[
                (horizon_data["日期值"] >= training_start) & (horizon_data["日期值"] <= cutoff)
            ]
            train_stats = {}
            eligible = []
            for model in candidate_models:
                train_ic = pd.to_numeric(
                    training.loc[training["模型"] == model, "秩相关IC"], errors="coerce"
                ).dropna()
                avg_ic = train_ic.mean() if len(train_ic) else np.nan
                positive = (train_ic > 0).mean() if len(train_ic) else np.nan
                train_stats[model] = (len(train_ic), avg_ic, positive)
                if len(train_ic) >= min_train_snapshots and avg_ic > 0 and positive >= min_positive_ratio:
                    eligible.append((model, avg_ic))

            enough_history = all(train_stats[model][0] >= min_train_snapshots for model in candidate_models)
            if not enough_history:
                continue
            selected = max(eligible, key=lambda item: item[1])[0] if eligible else "不启用"
            row = {
                "验证月份": month, "周期": horizon_text,
                "训练起始": training_start.strftime("%Y-%m-%d"),
                "信息截止": cutoff.strftime("%Y-%m-%d"),
                "选择模型": selected,
                "选择原因": "过去窗口方向达标且平均IC最高" if selected != "不启用" else "没有模型同时满足平均IC为正和正向比例门槛",
            }
            for model in candidate_models:
                count, avg_ic, positive = train_stats[model]
                validation = evaluation[evaluation["模型"] == model]
                row[f"{model}训练截面数"] = count
                row[f"{model}训练IC"] = avg_ic
                row[f"{model}训练正向比例"] = positive
                row[f"{model}验证IC"] = pd.to_numeric(validation["秩相关IC"], errors="coerce").mean()
                row[f"{model}验证Q5减Q1"] = pd.to_numeric(
                    validation["Q5减Q1同池超额"], errors="coerce"
                ).mean()
                row[f"{model}验证截面数"] = validation["秩相关IC"].notna().sum()
            if selected == "不启用":
                row["动态验证IC"] = np.nan
                row["动态验证Q5减Q1"] = np.nan
                row["动态验证截面数"] = 0
            else:
                row["动态验证IC"] = row[f"{selected}验证IC"]
                row["动态验证Q5减Q1"] = row[f"{selected}验证Q5减Q1"]
                row["动态验证截面数"] = row[f"{selected}验证截面数"]
            rows.append(row)
    return pd.DataFrame(rows)


def summarize_rolling(monthly: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if monthly.empty:
        return pd.DataFrame(rows)
    for horizon, period in monthly.groupby("周期", sort=False):
        series = {
            "趋势延续（固定）": (period["趋势延续验证IC"], period["趋势延续验证Q5减Q1"]),
            "回撤反弹（固定）": (period["回撤反弹验证IC"], period["回撤反弹验证Q5减Q1"]),
            "月度动态选择": (period["动态验证IC"], period["动态验证Q5减Q1"]),
        }
        for model, (ic_values, spread_values) in series.items():
            ic = pd.to_numeric(ic_values, errors="coerce").dropna()
            spread = pd.to_numeric(spread_values, errors="coerce").dropna()
            stable = (
                len(ic) >= 12 and ic.mean() > 0 and (ic > 0).mean() >= .55
                and len(spread) >= 12 and spread.mean() > 0 and (spread > 0).mean() >= .55
            )
            rows.append({
                "周期": horizon, "验证方式": model,
                "覆盖月份数": max(len(ic), len(spread)),
                "平均月度IC": ic.mean() if len(ic) else np.nan,
                "IC为正月份比例": (ic > 0).mean() if len(ic) else np.nan,
                "平均月度Q5减Q1": spread.mean() if len(spread) else np.nan,
                "Q5跑赢Q1月份比例": (spread > 0).mean() if len(spread) else np.nan,
                "结论": "初步通过滚动验证" if stable else "未通过滚动验证",
            })
    return pd.DataFrame(rows)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="机会因子诊断与月度滚动验证")
    parser.add_argument("--detail", help="机会评分回测明细；默认自动选择最新文件")
    parser.add_argument("--output-dir", default=config_value("files", "output_dir", "data/output"))
    parser.add_argument("--horizons", type=parse_horizons, default=parse_horizons("5,20,60"))
    parser.add_argument("--train-months", type=int, default=12)
    parser.add_argument("--min-train-snapshots", type=int, default=24)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.train_months <= 0 or args.min_train_snapshots <= 0:
        raise ValueError("训练月份和最少训练截面必须为正数")
    output_dir = project_path(args.output_dir)
    detail_path = project_path(args.detail) if args.detail else latest_matching_file(
        output_dir, "机会评分回测_明细_*.csv"
    )
    detail = read_csv_auto(detail_path)
    scored = add_minimal_model_scores(detail)
    metrics = build_snapshot_metrics(scored, args.horizons)
    diagnostics = summarize_diagnostics(metrics)
    monthly = rolling_monthly_validation(
        metrics, train_months=args.train_months, min_train_snapshots=args.min_train_snapshots,
    )
    summary = summarize_rolling(monthly)

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outputs = {
        "因子诊断": timestamped_output_path(output_dir, "机会因子诊断", timestamp=timestamp, suffix=".csv"),
        "滚动月度": timestamped_output_path(output_dir, "机会因子滚动月度", timestamp=timestamp, suffix=".csv"),
        "滚动汇总": timestamped_output_path(output_dir, "机会因子滚动汇总", timestamp=timestamp, suffix=".csv"),
    }
    for name, frame in (("因子诊断", diagnostics), ("滚动月度", monthly), ("滚动汇总", summary)):
        write_csv(frame, outputs[name])
    print(f"输入明细：{detail_path}")
    print(f"历史截面：{metrics['回测截面日'].nunique()} | 滚动月份：{monthly['验证月份'].nunique() if not monthly.empty else 0}")
    for name, path in outputs.items():
        print(f"{name}：{path}")
    print("\n滚动验证汇总")
    print(summary.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
