"""使用持久历史行情库对趋势分类做轻量历史回测。"""

from __future__ import annotations

import argparse
import importlib.util
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from classification_rules import classify_label
from history_store import load_history
from pipeline_config import config_value, project_path, resolve_input
from stock_utils import normalize_code, read_csv_auto, timestamped_output_path, write_csv


RATING_MODULE_PATH = Path(__file__).with_name("综合评级_安全缓存并发版(1).py")


def load_rating_module():
    spec = importlib.util.spec_from_file_location("rating_engine_for_backtest", RATING_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载评级模块：{RATING_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare_scored_history(rating, history: pd.DataFrame, benchmark: pd.DataFrame) -> pd.DataFrame:
    scored = rating.add_basic_features(history)
    scored = rating.add_atr(scored)
    scored = rating.add_adx(scored)
    scored = rating.add_donchian(scored)
    scored = rating.add_relative_strength(scored, benchmark)
    return rating.calc_scores(scored)


def parse_horizons(value: str) -> list[int]:
    horizons = sorted({int(item.strip()) for item in value.split(",") if item.strip()})
    if not horizons or any(item <= 0 for item in horizons):
        raise argparse.ArgumentTypeError("horizons 必须是逗号分隔的正整数")
    return horizons


def choose_snapshot_dates(
    benchmark: pd.DataFrame,
    *,
    minimum_history: int,
    max_horizon: int,
    snapshots: int,
    step: int,
) -> list[str]:
    if step <= 0:
        raise ValueError("step 必须为正整数")
    dates = benchmark.sort_values("date")["date"].astype(str).drop_duplicates().tolist()
    last_position = len(dates) - max_horizon - 1
    first_position = minimum_history - 1
    if snapshots <= 0:
        count = max(0, (last_position - first_position) // step + 1)
    else:
        count = snapshots
    positions = [last_position - step * offset for offset in range(count)]
    positions = sorted(position for position in positions if position >= first_position)
    if not positions:
        raise RuntimeError("历史库覆盖长度不足，无法同时满足指标窗口和未来收益窗口")
    return [dates[position] for position in positions]


def benchmark_forward_returns(
    benchmark: pd.DataFrame,
    snapshot_dates: list[str],
    horizons: list[int],
) -> dict[tuple[str, int], float]:
    frame = benchmark.copy().sort_values("date").reset_index(drop=True)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    result: dict[tuple[str, int], float] = {}
    for snapshot in snapshot_dates:
        eligible = frame.index[frame["date"].astype(str) <= snapshot]
        if len(eligible) == 0:
            continue
        position = int(eligible[-1])
        for horizon in horizons:
            if position + horizon < len(frame):
                result[(snapshot, horizon)] = (
                    frame.at[position + horizon, "close"] / frame.at[position, "close"] - 1
                )
    return result


def trimmed_mean(values: pd.Series, proportion: float = 0.1) -> float:
    values = pd.to_numeric(values, errors="coerce").dropna().sort_values()
    if values.empty:
        return np.nan
    cut = int(len(values) * proportion)
    trimmed = values.iloc[cut:len(values) - cut] if cut else values
    return float(trimmed.mean())


def add_pool_relative_returns(detail: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    result = detail.copy()
    for horizon in horizons:
        return_column = f"未来{horizon}日收益"
        pool_mean_column = f"同池{horizon}日平均收益"
        pool_excess_column = f"未来{horizon}日同池超额"
        returns = pd.to_numeric(result[return_column], errors="coerce")
        pool_means = returns.groupby(result["回测截面日"]).transform("mean")
        result[pool_mean_column] = pool_means
        result[pool_excess_column] = returns - pool_means
    return result


def stock_contribution_ratio(subset: pd.DataFrame, value_column: str) -> float:
    values = pd.to_numeric(subset[value_column], errors="coerce")
    contributions = values.groupby(subset["代码"]).sum(min_count=1).abs().dropna()
    total = contributions.sum()
    return float(contributions.max() / total) if len(contributions) and total > 0 else np.nan


def quality_assessment(
    *, samples: int, stocks: int, dates: int, contribution: float,
    mean_return: float, median_return: float, overlapping: bool,
) -> tuple[str, str]:
    warnings = []
    if samples < 100:
        warnings.append("样本少于100")
    if stocks < 50:
        warnings.append("股票少于50只")
    if dates < 10:
        warnings.append("截面少于10个")
    if pd.notna(contribution) and contribution > 0.20:
        warnings.append("单股绝对贡献超过20%")
    if pd.notna(mean_return) and pd.notna(median_return) and abs(mean_return - median_return) > 0.10:
        warnings.append("均值与中位数偏离超过10个百分点")
    if overlapping:
        warnings.append("未来收益窗口存在重叠")
    if any(item in warnings for item in ("样本少于100", "股票少于50只", "截面少于10个")):
        level = "低"
    elif warnings:
        level = "中"
    else:
        level = "高"
    return level, "；".join(warnings) if warnings else "数据质量良好"


def summarize(detail: pd.DataFrame, horizons: list[int], *, step: int = 20) -> pd.DataFrame:
    rows = []
    for label in sorted(detail["分类"].dropna().unique()):
        subset = detail[detail["分类"] == label]
        for horizon in horizons:
            returns = pd.to_numeric(subset[f"未来{horizon}日收益"], errors="coerce").dropna()
            excess = pd.to_numeric(subset[f"未来{horizon}日超额"], errors="coerce").dropna()
            pool_excess_column = f"未来{horizon}日同池超额"
            pool_excess = pd.to_numeric(subset[pool_excess_column], errors="coerce").dropna()
            date_excess = (
                subset.assign(_pool_excess=pd.to_numeric(subset[pool_excess_column], errors="coerce"))
                .groupby("回测截面日")["_pool_excess"].mean().dropna()
            )
            sample_count = int(len(returns))
            stock_count = int(subset.loc[returns.index, "代码"].nunique()) if sample_count else 0
            date_count = int(subset.loc[returns.index, "回测截面日"].nunique()) if sample_count else 0
            mean_return = returns.mean() if sample_count else np.nan
            median_return = returns.median() if sample_count else np.nan
            contribution = stock_contribution_ratio(subset.loc[returns.index], pool_excess_column)
            quality, quality_note = quality_assessment(
                samples=sample_count,
                stocks=stock_count,
                dates=date_count,
                contribution=contribution,
                mean_return=mean_return,
                median_return=median_return,
                overlapping=step < horizon,
            )
            rows.append(
                {
                    "分类": label,
                    "周期": f"{horizon}日",
                    "截面间隔": step,
                    "样本数": sample_count,
                    "不同股票数": stock_count,
                    "覆盖截面数": date_count,
                    "平均收益": mean_return,
                    "中位收益": median_return,
                    "10%截尾均值": trimmed_mean(returns),
                    "上涨胜率": (returns > 0).mean() if sample_count else np.nan,
                    "平均超额": excess.mean() if len(excess) else np.nan,
                    "跑赢基准率": (excess > 0).mean() if len(excess) else np.nan,
                    "平均同池超额": pool_excess.mean() if len(pool_excess) else np.nan,
                    "跑赢同池率": (pool_excess > 0).mean() if len(pool_excess) else np.nan,
                    "截面跑赢同池率": (date_excess > 0).mean() if len(date_excess) else np.nan,
                    "最差截面同池超额": date_excess.min() if len(date_excess) else np.nan,
                    "最好截面同池超额": date_excess.max() if len(date_excess) else np.nan,
                    "最大单股绝对贡献占比": contribution,
                    "窗口是否重叠": "是" if step < horizon else "否",
                    "可信度": quality,
                    "数据质量提示": quality_note,
                }
            )
    return pd.DataFrame(rows)


def run_backtest(args) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    rating = load_rating_module()
    history_dir = project_path(args.history_dir)
    pool_file = resolve_input(args.pool, config_key="stock_pool")
    pool = read_csv_auto(pool_file, dtype=str)
    if "代码" not in pool.columns:
        raise ValueError(f"股票池缺少【代码】列：{pool_file}")

    codes = pool["代码"].dropna().astype(str).drop_duplicates().tolist()
    if args.max_stocks > 0 and len(codes) > args.max_stocks:
        positions = np.linspace(0, len(codes) - 1, args.max_stocks, dtype=int)
        codes = [codes[position] for position in positions]

    benchmark = load_history(
        history_dir,
        rating.BENCHMARK_CODE,
        kind="benchmark",
        verify_checksum=not args.no_verify_history,
    )
    if benchmark is None or benchmark.empty:
        raise FileNotFoundError(
            f"正式历史库中缺少基准指数：{rating.BENCHMARK_CODE}；"
            "请先运行 migrate_cache_to_history.py 或主评级"
        )

    horizons = args.horizons
    snapshot_dates = choose_snapshot_dates(
        benchmark,
        minimum_history=rating.MIN_EFFECTIVE_ROWS,
        max_horizon=max(horizons),
        snapshots=args.snapshots,
        step=args.step,
    )
    benchmark_returns = benchmark_forward_returns(benchmark, snapshot_dates, horizons)

    records = []
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
        for snapshot in snapshot_dates:
            eligible = scored.index[scored["date"].astype(str) <= snapshot]
            if len(eligible) < rating.MIN_EFFECTIVE_ROWS:
                continue
            position = int(eligible[-1])
            if position + max(horizons) >= len(scored):
                continue

            row = scored.iloc[position]
            label = classify_label(row)
            record = {
                "代码": normalize_code(code, "suffix"),
                "回测截面日": snapshot,
                "实际信号日": str(row["date"]),
                "分类": label,
                "信号收盘价": float(row["close"]),
                "trend_score": row.get("trend_score"),
                "direction_score": row.get("direction_score"),
                "position_score": row.get("position_score"),
                "exhaustion_score": row.get("exhaustion_score"),
            }
            for horizon in horizons:
                future_return = scored.iloc[position + horizon]["close"] / row["close"] - 1
                bench_return = benchmark_returns.get((snapshot, horizon), np.nan)
                record[f"未来{horizon}日收益"] = future_return
                record[f"基准{horizon}日收益"] = bench_return
                record[f"未来{horizon}日超额"] = future_return - bench_return
            records.append(record)

        if stock_number % 10 == 0:
            print(f"已处理 {stock_number}/{len(codes)} 只股票", flush=True)

    detail = pd.DataFrame(records)
    if detail.empty:
        raise RuntimeError("没有生成可回测样本，请检查历史库覆盖范围和参数")
    detail = add_pool_relative_returns(detail, horizons)
    return detail, summarize(detail, horizons, step=args.step), stats


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="使用不可清理的正式历史行情库进行趋势分类回测",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--pool", help="股票池 CSV；默认使用统一配置")
    parser.add_argument(
        "--history-dir",
        default=config_value("files", "history_dir", "data/history"),
        help="正式历史行情目录",
    )
    parser.add_argument(
        "--no-verify-history",
        action="store_true",
        help="跳过 manifest 中的 SHA-256 完整性校验",
    )
    parser.add_argument(
        "--output-dir",
        default=config_value("files", "output_dir", "data/output"),
        help="输出目录",
    )
    parser.add_argument("--max-stocks", type=int, default=50, help="最多抽样股票数；0 表示全部")
    parser.add_argument("--snapshots", type=int, default=3, help="历史截面数量")
    parser.add_argument("--step", type=int, default=20, help="截面间隔交易日数")
    parser.add_argument("--horizons", type=parse_horizons, default=parse_horizons("5,20,60"))
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    detail, summary, stats = run_backtest(args)
    output_dir = project_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    detail_file = timestamped_output_path(
        output_dir, "分类历史回测_明细", timestamp=timestamp, suffix=".csv"
    )
    summary_file = timestamped_output_path(
        output_dir, "分类历史回测_汇总", timestamp=timestamp, suffix=".csv"
    )
    write_csv(detail, detail_file)
    write_csv(summary, summary_file)

    print("\n回测完成")
    print(f"明细：{detail_file}")
    print(f"汇总：{summary_file}")
    print(f"样本记录：{len(detail)}")
    print(f"缺少历史行情：{stats['missing_history']}")
    print(f"分析失败：{stats['analysis_failures']}")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
