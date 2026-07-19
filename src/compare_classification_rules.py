"""在完全相同的股票和历史截面上比较基线与候选分类规则。"""

from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from backtest_classification import (
    add_pool_relative_returns,
    benchmark_forward_returns,
    choose_snapshot_dates,
    load_rating_module,
    parse_horizons,
    prepare_scored_history,
    quality_assessment,
    stock_contribution_ratio,
    trimmed_mean,
)
from classification_rules import (
    CURRENT_RULES,
    REQUIRED_CLASSIFICATION_FIELDS,
    RuleConfig,
    classify_label,
    rule_config_from_mapping,
)
from history_store import load_history
from pipeline_config import PROJECT_DIR, config_value, project_path, resolve_input
from stock_utils import normalize_code, read_csv_auto, timestamped_output_path, write_csv


DEFAULT_CONFIG_FILE = PROJECT_DIR / "classification_rule_configs.toml"


def rule_hash(config: RuleConfig) -> str:
    payload = json.dumps(asdict(config), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_rule_configs(
    path: str | Path, selected: list[str] | None = None
) -> tuple[dict[str, RuleConfig], dict[str, str]]:
    config_path = project_path(path)
    with config_path.open("rb") as handle:
        payload = tomllib.load(handle)
    candidates = payload.get("candidates", {})
    if not isinstance(candidates, dict) or not candidates:
        raise ValueError(f"候选规则配置为空：{config_path}")

    wanted = set(selected or candidates.keys())
    missing = sorted(wanted - set(candidates))
    if missing:
        raise KeyError(f"配置中没有候选规则：{missing}")

    rules = {"baseline": CURRENT_RULES}
    descriptions = {"baseline": "当前生产分类规则"}
    for name, raw in candidates.items():
        if name not in wanted:
            continue
        values = dict(raw)
        descriptions[name] = str(values.pop("description", name))
        rules[name] = rule_config_from_mapping(values)
    return rules, descriptions


def assign_time_segments(snapshot_dates: list[str]) -> dict[str, str]:
    """按日期顺序做60%/20%/20%切分，避免随机拆分造成未来信息泄漏。"""
    dates = sorted(set(snapshot_dates))
    result = {}
    total = len(dates)
    for index, date in enumerate(dates):
        ratio = (index + 1) / total
        if ratio <= 0.60:
            result[date] = "训练期"
        elif ratio <= 0.80:
            result[date] = "验证期"
        else:
            result[date] = "测试期"
    return result


def bootstrap_mean_ci(
    values: pd.Series, *, iterations: int, rng: np.random.Generator
) -> tuple[float, float]:
    numbers = pd.to_numeric(values, errors="coerce").dropna().to_numpy(dtype=float)
    if len(numbers) < 2 or iterations <= 0:
        return np.nan, np.nan
    indices = rng.integers(0, len(numbers), size=(iterations, len(numbers)))
    means = numbers[indices].mean(axis=1)
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def bootstrap_snapshot_mean_ci(
    subset: pd.DataFrame,
    value_column: str,
    *,
    iterations: int,
    rng: np.random.Generator,
) -> tuple[float, float]:
    """按截面而非单只股票重采样，避免夸大同日样本的独立性。"""
    values = pd.to_numeric(subset[value_column], errors="coerce")
    snapshot_means = values.groupby(subset["回测截面日"]).mean().dropna()
    return bootstrap_mean_ci(snapshot_means, iterations=iterations, rng=rng)


def _sample_codes(codes: list[str], max_stocks: int) -> list[str]:
    if max_stocks <= 0 or len(codes) <= max_stocks:
        return codes
    positions = np.linspace(0, len(codes) - 1, max_stocks, dtype=int)
    return [codes[position] for position in positions]


def build_comparison_samples(
    args, rules: dict[str, RuleConfig]
) -> tuple[pd.DataFrame, dict[str, int], list[str]]:
    rating = load_rating_module()
    history_dir = project_path(args.history_dir)
    pool_file = resolve_input(args.pool, config_key="stock_pool")
    pool = read_csv_auto(pool_file, dtype=str)
    if "代码" not in pool.columns:
        raise ValueError(f"股票池缺少【代码】列：{pool_file}")
    codes = _sample_codes(
        pool["代码"].dropna().astype(str).drop_duplicates().tolist(), args.max_stocks
    )

    benchmark = load_history(
        history_dir,
        rating.BENCHMARK_CODE,
        kind="benchmark",
        verify_checksum=not args.no_verify_history,
    )
    if benchmark is None or benchmark.empty:
        raise FileNotFoundError(f"正式历史库缺少基准指数：{rating.BENCHMARK_CODE}")

    horizons = args.horizons
    snapshot_dates = choose_snapshot_dates(
        benchmark,
        minimum_history=rating.MIN_EFFECTIVE_ROWS,
        max_horizon=max(horizons),
        snapshots=args.snapshots,
        step=args.step,
    )
    period_by_date = assign_time_segments(snapshot_dates)
    benchmark_returns = benchmark_forward_returns(benchmark, snapshot_dates, horizons)

    records: list[dict] = []
    stats = {"requested_stocks": len(codes), "missing_history": 0, "analysis_failures": 0}
    for stock_number, code in enumerate(codes, start=1):
        history = load_history(
            history_dir,
            code,
            kind="daily",
            verify_checksum=not args.no_verify_history,
        )
        if history is None or history.empty:
            stats["missing_history"] += 1
            continue
        try:
            scored = prepare_scored_history(rating, history, benchmark)
        except Exception:
            stats["analysis_failures"] += 1
            continue

        scored = scored.sort_values("date").reset_index(drop=True)
        scored["close"] = pd.to_numeric(scored["close"], errors="coerce")
        for snapshot in snapshot_dates:
            eligible = scored.index[scored["date"].astype(str) <= snapshot]
            if len(eligible) < rating.MIN_EFFECTIVE_ROWS:
                continue
            position = int(eligible[-1])
            if position + max(horizons) >= len(scored):
                continue
            row = scored.iloc[position]
            if pd.isna(row["close"]):
                continue

            record = {
                "代码": normalize_code(code, "suffix"),
                "回测截面日": snapshot,
                "实际信号日": str(row["date"]),
                "样本区间": period_by_date[snapshot],
                "信号收盘价": float(row["close"]),
            }
            for field in REQUIRED_CLASSIFICATION_FIELDS:
                record[field] = row.get(field)
            for name, config in rules.items():
                record[f"{name}分类"] = classify_label(row, config)
            baseline_label = record["baseline分类"]
            for name in rules:
                if name != "baseline":
                    record[f"{name}是否变化"] = record[f"{name}分类"] != baseline_label

            for horizon in horizons:
                future = scored.iloc[position + 1 : position + horizon + 1]["close"].dropna()
                end_close = float(scored.iloc[position + horizon]["close"])
                stock_return = end_close / float(row["close"]) - 1
                bench_return = benchmark_returns.get((snapshot, horizon), np.nan)
                record[f"未来{horizon}日收益"] = stock_return
                record[f"基准{horizon}日收益"] = bench_return
                record[f"未来{horizon}日超额"] = stock_return - bench_return
                record[f"未来{horizon}日最大回撤"] = (
                    future.min() / float(row["close"]) - 1 if len(future) else np.nan
                )
                record[f"未来{horizon}日最大涨幅"] = (
                    future.max() / float(row["close"]) - 1 if len(future) else np.nan
                )
            records.append(record)

        if stock_number % 25 == 0 or stock_number == len(codes):
            print(f"已处理 {stock_number}/{len(codes)} 只股票", flush=True)

    detail = pd.DataFrame(records)
    if detail.empty:
        raise RuntimeError("没有生成规则比较样本，请检查历史覆盖和参数")
    return add_pool_relative_returns(detail, horizons), stats, snapshot_dates


def to_long_labels(detail: pd.DataFrame, rules: dict[str, RuleConfig]) -> pd.DataFrame:
    common = [column for column in detail.columns if not column.endswith(("分类", "是否变化"))]
    frames = []
    for name in rules:
        frame = detail[common].copy()
        frame["规则"] = name
        frame["分类"] = detail[f"{name}分类"]
        frame["相对基线发生变化"] = (
            False if name == "baseline" else detail[f"{name}是否变化"]
        )
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def summarize_performance(
    long_detail: pd.DataFrame,
    horizons: list[int],
    *,
    min_samples: int,
    bootstrap_iterations: int,
    seed: int,
    step: int = 5,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    periods = [("总体", long_detail)] + [
        (name, group) for name, group in long_detail.groupby("样本区间", sort=False)
    ]
    for period_name, period_frame in periods:
        for (rule_name, label), subset in period_frame.groupby(["规则", "分类"], sort=True):
            for horizon in horizons:
                returns = pd.to_numeric(subset[f"未来{horizon}日收益"], errors="coerce").dropna()
                excess = pd.to_numeric(subset[f"未来{horizon}日超额"], errors="coerce").dropna()
                pool_excess_column = f"未来{horizon}日同池超额"
                pool_excess = pd.to_numeric(subset[pool_excess_column], errors="coerce").dropna()
                drawdown = pd.to_numeric(
                    subset[f"未来{horizon}日最大回撤"], errors="coerce"
                ).dropna()
                ci_low, ci_high = bootstrap_snapshot_mean_ci(
                    subset,
                    pool_excess_column,
                    iterations=bootstrap_iterations,
                    rng=rng,
                )
                sample_count = int(len(returns))
                stock_count = int(subset.loc[returns.index, "代码"].nunique()) if sample_count else 0
                date_count = int(subset.loc[returns.index, "回测截面日"].nunique()) if sample_count else 0
                mean_return = returns.mean() if sample_count else np.nan
                median_return = returns.median() if sample_count else np.nan
                contribution = stock_contribution_ratio(
                    subset.loc[returns.index], pool_excess_column
                )
                quality, quality_note = quality_assessment(
                    samples=sample_count,
                    stocks=stock_count,
                    dates=date_count,
                    contribution=contribution,
                    mean_return=mean_return,
                    median_return=median_return,
                    overlapping=step < horizon,
                )
                significance = (
                    "同池超额显著为正" if pd.notna(ci_low) and ci_low > 0
                    else "同池超额显著为负" if pd.notna(ci_high) and ci_high < 0
                    else "同池超额尚不显著"
                )
                rows.append(
                    {
                        "样本区间": period_name,
                        "规则": rule_name,
                        "分类": label,
                        "周期": f"{horizon}日",
                        "截面间隔": step,
                        "样本数": sample_count,
                        "不同股票数": stock_count,
                        "覆盖截面数": date_count,
                        "样本充足": sample_count >= min_samples,
                        "平均收益": mean_return,
                        "中位收益": median_return,
                        "10%截尾均值": trimmed_mean(returns),
                        "上涨胜率": (returns > 0).mean() if len(returns) else np.nan,
                        "平均超额": excess.mean() if len(excess) else np.nan,
                        "中位超额": excess.median() if len(excess) else np.nan,
                        "跑赢基准率": (excess > 0).mean() if len(excess) else np.nan,
                        "平均同池超额": pool_excess.mean() if len(pool_excess) else np.nan,
                        "跑赢同池率": (pool_excess > 0).mean() if len(pool_excess) else np.nan,
                        "平均信号期最大回撤": drawdown.mean() if len(drawdown) else np.nan,
                        "同池超额95%CI下限": ci_low,
                        "同池超额95%CI上限": ci_high,
                        "统计结论": significance,
                        "最大单股绝对贡献占比": contribution,
                        "窗口是否重叠": "是" if step < horizon else "否",
                        "可信度": quality,
                        "数据质量提示": quality_note,
                    }
                )
    return pd.DataFrame(rows)


def summarize_coverage(long_detail: pd.DataFrame) -> pd.DataFrame:
    rows = []
    total_per_rule = long_detail.groupby("规则").size().to_dict()
    for (rule_name, label), subset in long_detail.groupby(["规则", "分类"], sort=True):
        total = total_per_rule[rule_name]
        rows.append(
            {
                "规则": rule_name,
                "分类": label,
                "样本数": len(subset),
                "占比": len(subset) / total,
                "相对基线变化样本数": int(subset["相对基线发生变化"].sum()),
            }
        )
    return pd.DataFrame(rows)


def summarize_stability(long_detail: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for rule_name, rule_frame in long_detail.groupby("规则", sort=True):
        comparisons = changes = 0
        for _, stock in rule_frame.sort_values("回测截面日").groupby("代码"):
            labels = stock["分类"].tolist()
            comparisons += max(0, len(labels) - 1)
            changes += sum(left != right for left, right in zip(labels, labels[1:]))
        rows.append(
            {
                "规则": rule_name,
                "相邻截面比较数": comparisons,
                "分类变化次数": changes,
                "分类变化率": changes / comparisons if comparisons else np.nan,
                "边界模糊率": (rule_frame["分类"] == "边界模糊").mean(),
                "相对基线变化率": rule_frame["相对基线发生变化"].mean(),
            }
        )
    return pd.DataFrame(rows)


def build_baseline_deltas(performance: pd.DataFrame) -> pd.DataFrame:
    """按同一时间段、分类和周期，计算候选统计值减去基线统计值。"""
    keys = ["样本区间", "分类", "周期"]
    metrics = [
        "样本数",
        "平均收益",
        "中位收益",
        "上涨胜率",
        "平均超额",
        "中位超额",
        "跑赢基准率",
        "平均同池超额",
        "跑赢同池率",
        "平均信号期最大回撤",
    ]
    baseline = performance[performance["规则"] == "baseline"][keys + metrics]
    frames = []
    for name in sorted(set(performance["规则"]) - {"baseline"}):
        candidate = performance[performance["规则"] == name][keys + metrics]
        merged = candidate.merge(baseline, on=keys, how="outer", suffixes=("_候选", "_基线"))
        result = merged[keys].copy()
        result.insert(1, "候选规则", name)
        for metric in metrics:
            result[f"候选减基线_{metric}"] = (
                pd.to_numeric(merged[f"{metric}_候选"], errors="coerce")
                - pd.to_numeric(merged[f"{metric}_基线"], errors="coerce")
            )
        frames.append(result)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def build_change_matrix(detail: pd.DataFrame, rules: dict[str, RuleConfig]) -> pd.DataFrame:
    frames = []
    baseline = detail["baseline分类"]
    for name in rules:
        if name == "baseline":
            continue
        matrix = pd.crosstab(baseline, detail[f"{name}分类"], dropna=False)
        melted = matrix.rename_axis("基线分类").reset_index().melt(
            id_vars="基线分类", var_name="候选分类", value_name="样本数"
        )
        melted.insert(0, "候选规则", name)
        frames.append(melted[melted["样本数"] > 0])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def summarize_changed_samples(
    detail: pd.DataFrame, rules: dict[str, RuleConfig], horizons: list[int]
) -> pd.DataFrame:
    rows = []
    for name in rules:
        if name == "baseline":
            continue
        changed = detail[detail[f"{name}是否变化"]]
        for (old_label, new_label), subset in changed.groupby(
            ["baseline分类", f"{name}分类"], sort=True
        ):
            for horizon in horizons:
                returns = pd.to_numeric(subset[f"未来{horizon}日收益"], errors="coerce").dropna()
                excess = pd.to_numeric(subset[f"未来{horizon}日超额"], errors="coerce").dropna()
                rows.append(
                    {
                        "候选规则": name,
                        "基线分类": old_label,
                        "候选分类": new_label,
                        "周期": f"{horizon}日",
                        "样本数": len(returns),
                        "平均收益": returns.mean() if len(returns) else np.nan,
                        "中位收益": returns.median() if len(returns) else np.nan,
                        "平均超额": excess.mean() if len(excess) else np.nan,
                        "跑赢基准率": (excess > 0).mean() if len(excess) else np.nan,
                    }
                )
    return pd.DataFrame(rows)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="在相同历史样本上比较当前分类规则和候选规则",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--pool", help="股票池CSV；默认使用统一配置")
    parser.add_argument(
        "--history-dir", default=config_value("files", "history_dir", "data/history")
    )
    parser.add_argument(
        "--output-dir", default=config_value("files", "output_dir", "data/output")
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_FILE), help="候选规则TOML")
    parser.add_argument(
        "--candidate", action="append", help="只运行指定候选；可重复传入"
    )
    parser.add_argument("--max-stocks", type=int, default=100, help="最多股票数；0为全部")
    parser.add_argument("--snapshots", type=int, default=12, help="截面数；0为全部可用截面")
    parser.add_argument("--step", type=int, default=5, help="截面间隔交易日")
    parser.add_argument("--horizons", type=parse_horizons, default=parse_horizons("5,20,60"))
    parser.add_argument("--min-samples", type=int, default=30, help="分类统计最小可信样本数")
    parser.add_argument("--bootstrap-iterations", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20260714)
    parser.add_argument("--no-verify-history", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.step <= 0 or args.min_samples <= 0 or args.bootstrap_iterations < 0:
        raise ValueError("step和min-samples必须为正数，bootstrap-iterations不能为负数")
    rules, descriptions = load_rule_configs(args.config, args.candidate)
    detail, stats, snapshot_dates = build_comparison_samples(args, rules)
    long_detail = to_long_labels(detail, rules)
    performance = summarize_performance(
        long_detail,
        args.horizons,
        min_samples=args.min_samples,
        bootstrap_iterations=args.bootstrap_iterations,
        seed=args.seed,
        step=args.step,
    )
    coverage = summarize_coverage(long_detail)
    stability = summarize_stability(long_detail)
    deltas = build_baseline_deltas(performance)
    matrix = build_change_matrix(detail, rules)
    changed = summarize_changed_samples(detail, rules, args.horizons)

    output_dir = project_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outputs = {
        "明细": timestamped_output_path(output_dir, "分类规则对比_明细", timestamp=timestamp, suffix=".csv"),
        "表现": timestamped_output_path(output_dir, "分类规则对比_表现", timestamp=timestamp, suffix=".csv"),
        "覆盖率": timestamped_output_path(output_dir, "分类规则对比_覆盖率", timestamp=timestamp, suffix=".csv"),
        "稳定性": timestamped_output_path(output_dir, "分类规则对比_稳定性", timestamp=timestamp, suffix=".csv"),
        "基线差异": timestamped_output_path(output_dir, "分类规则对比_基线差异", timestamp=timestamp, suffix=".csv"),
        "变化矩阵": timestamped_output_path(output_dir, "分类规则对比_变化矩阵", timestamp=timestamp, suffix=".csv"),
        "变化样本": timestamped_output_path(output_dir, "分类规则对比_变化样本", timestamp=timestamp, suffix=".csv"),
    }
    for label, frame in (
        ("明细", detail),
        ("表现", performance),
        ("覆盖率", coverage),
        ("稳定性", stability),
        ("基线差异", deltas),
        ("变化矩阵", matrix),
        ("变化样本", changed),
    ):
        write_csv(frame, outputs[label])

    metadata = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "config_file": str(project_path(args.config)),
        "snapshot_dates": snapshot_dates,
        "horizons": args.horizons,
        "step": args.step,
        "snapshots_requested": args.snapshots,
        "max_stocks": args.max_stocks,
        "statistics": stats,
        "rules": {
            name: {
                "description": descriptions[name],
                "sha256": rule_hash(config),
                "parameters": asdict(config),
            }
            for name, config in rules.items()
        },
        "outputs": {key: str(value) for key, value in outputs.items()},
    }
    metadata_path = timestamped_output_path(
        output_dir, "分类规则对比_元数据", timestamp=timestamp, suffix=".json"
    )
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n规则对比完成")
    print(f"样本：{len(detail)} | 截面：{len(snapshot_dates)}")
    print(f"缺少历史：{stats['missing_history']} | 分析失败：{stats['analysis_failures']}")
    for label, path in outputs.items():
        print(f"{label}：{path}")
    print(f"元数据：{metadata_path}")
    print("\n稳定性概览")
    print(stability.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
