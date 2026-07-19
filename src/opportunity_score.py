"""透明、与趋势分类解耦的机会评分。评分用于研究排序，不改变分类结果。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Any

import numpy as np
import pandas as pd

from pipeline_config import PROJECT_DIR, project_path
from stock_utils import normalize_code


DEFAULT_CONFIG = PROJECT_DIR / "opportunity_score.toml"
SCORE_VERSION = "v1.0-transparent"
KNOWN_INDEX_CODES = {
    "000001.SH", "000016.SH", "000300.SH", "000688.SH", "000905.SH",
    "399001.SZ", "399006.SZ"
}
OPPORTUNITY_SCORE_FIELDS = (
    "trend_score", "direction_score", "rs_score", "breakout_score",
    "ma_structure_score", "adx_score", "trend_stability_score", "volume_score",
    "stabilize_score", "base_score", "exhaustion_score", "position_score",
    "stall_score", "ATR_ratio",
)


@dataclass(frozen=True)
class OpportunityConfig:
    trend_weight: float = 0.30
    relative_strength_weight: float = 0.25
    breakout_weight: float = 0.15
    confirmation_weight: float = 0.20
    setup_weight: float = 0.10
    exhaustion_start: float = 70
    exhaustion_max_penalty: float = 15
    position_start: float = 88
    position_max_penalty: float = 5
    stall_start: float = 70
    stall_max_penalty: float = 8
    atr_start: float = 0.04
    atr_full_penalty: float = 0.10
    atr_max_penalty: float = 7
    market_max_adjustment: float = 10
    high_level: float = 75
    watch_level: float = 60
    neutral_level: float = 45


def load_opportunity_config(path: str | Path = DEFAULT_CONFIG) -> OpportunityConfig:
    config_path = project_path(path)
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    weights, risk = raw.get("weights", {}), raw.get("risk", {})
    market, levels = raw.get("market", {}), raw.get("levels", {})
    config = OpportunityConfig(
        trend_weight=float(weights.get("trend", 0.30)),
        relative_strength_weight=float(weights.get("relative_strength", 0.25)),
        breakout_weight=float(weights.get("breakout", 0.15)),
        confirmation_weight=float(weights.get("confirmation", 0.20)),
        setup_weight=float(weights.get("setup", 0.10)),
        exhaustion_start=float(risk.get("exhaustion_start", 70)),
        exhaustion_max_penalty=float(risk.get("exhaustion_max_penalty", 15)),
        position_start=float(risk.get("position_start", 88)),
        position_max_penalty=float(risk.get("position_max_penalty", 5)),
        stall_start=float(risk.get("stall_start", 70)),
        stall_max_penalty=float(risk.get("stall_max_penalty", 8)),
        atr_start=float(risk.get("atr_start", 0.04)),
        atr_full_penalty=float(risk.get("atr_full_penalty", 0.10)),
        atr_max_penalty=float(risk.get("atr_max_penalty", 7)),
        market_max_adjustment=float(market.get("max_adjustment", 10)),
        high_level=float(levels.get("high", 75)),
        watch_level=float(levels.get("watch", 60)),
        neutral_level=float(levels.get("neutral", 45)),
    )
    weight_sum = sum((
        config.trend_weight, config.relative_strength_weight,
        config.breakout_weight, config.confirmation_weight, config.setup_weight,
    ))
    if not np.isclose(weight_sum, 1.0):
        raise ValueError(f"机会评分正向权重之和必须为1，当前为{weight_sum:.4f}")
    if not (config.high_level > config.watch_level > config.neutral_level):
        raise ValueError("机会等级阈值必须满足 high > watch > neutral")
    return config


def _number(row: Mapping[str, Any], field: str) -> float:
    value = pd.to_numeric(pd.Series([row.get(field)]), errors="coerce").iloc[0]
    return float(value) if pd.notna(value) else np.nan


def _clip(value: float, low: float = 0, high: float = 100) -> float:
    return float(np.clip(value, low, high))


def _signed_to_100(value: float) -> float:
    return _clip((value + 100) / 2) if pd.notna(value) else np.nan


def _mean_available(values: list[tuple[float, float]]) -> float:
    available = [(value, weight) for value, weight in values if pd.notna(value)]
    if not available:
        return np.nan
    total_weight = sum(weight for _, weight in available)
    return sum(value * weight for value, weight in available) / total_weight


def market_adjustment(
    market_metrics: Mapping[str, Any] | None,
    config: OpportunityConfig,
) -> tuple[float, float]:
    """用沪深300趋势与方向计算[-max,+max]的大盘环境调整。"""
    if not market_metrics:
        return 0.0, np.nan
    trend = _number(market_metrics, "trend_score")
    direction = _signed_to_100(_number(market_metrics, "direction_score"))
    ma_structure = _signed_to_100(_number(market_metrics, "ma_structure_score"))
    market_score = _mean_available([(trend, 0.50), (direction, 0.30), (ma_structure, 0.20)])
    if pd.isna(market_score):
        return 0.0, np.nan
    adjustment = (market_score - 50) / 50 * config.market_max_adjustment
    return _clip(adjustment, -config.market_max_adjustment, config.market_max_adjustment), market_score


def _linear_penalty(value: float, start: float, full: float, maximum: float) -> float:
    if pd.isna(value) or value <= start or full <= start:
        return 0.0
    return _clip((value - start) / (full - start) * maximum, 0, maximum)


def opportunity_level(score: float, config: OpportunityConfig) -> str:
    if pd.isna(score):
        return "数据不足"
    if score >= config.high_level:
        return "重点关注"
    if score >= config.watch_level:
        return "偏强观察"
    if score >= config.neutral_level:
        return "中性"
    return "偏弱"


def score_opportunity(
    row: Mapping[str, Any],
    *,
    market_metrics: Mapping[str, Any] | None = None,
    config: OpportunityConfig | None = None,
) -> dict[str, Any]:
    config = config or load_opportunity_config()
    trend_score = _number(row, "trend_score")
    direction = _signed_to_100(_number(row, "direction_score"))
    rs = _signed_to_100(_number(row, "rs_score"))
    breakout = _signed_to_100(_number(row, "breakout_score"))
    ma_structure = _signed_to_100(_number(row, "ma_structure_score"))
    adx = _number(row, "adx_score")
    stability = _number(row, "trend_stability_score")
    volume = _number(row, "volume_score")
    stabilize = _number(row, "stabilize_score")
    base = _number(row, "base_score")

    component_values = {
        "趋势质量": _mean_available([(trend_score, 0.65), (direction, 0.35)]),
        "相对强弱": rs,
        "突破动能": _mean_available([(breakout, 0.70), (volume, 0.30)]),
        "趋势确认": _mean_available([(ma_structure, 0.40), (adx, 0.35), (stability, 0.25)]),
        "形态准备": _mean_available([(stabilize, 0.65), (base, 0.35)]),
    }
    component_weights = {
        "趋势质量": config.trend_weight,
        "相对强弱": config.relative_strength_weight,
        "突破动能": config.breakout_weight,
        "趋势确认": config.confirmation_weight,
        "形态准备": config.setup_weight,
    }
    available_weight = sum(
        component_weights[name] for name, value in component_values.items() if pd.notna(value)
    )
    coverage = available_weight
    if available_weight < 0.60:
        score = np.nan
        positive_contributions = {name: np.nan for name in component_values}
    else:
        positive_contributions = {
            name: value * component_weights[name] / available_weight
            for name, value in component_values.items() if pd.notna(value)
        }
        score = sum(positive_contributions.values())

    exhaustion_penalty = _linear_penalty(
        _number(row, "exhaustion_score"), config.exhaustion_start, 100,
        config.exhaustion_max_penalty,
    )
    position_penalty = _linear_penalty(
        _number(row, "position_score"), config.position_start, 100,
        config.position_max_penalty,
    )
    stall_penalty = _linear_penalty(
        _number(row, "stall_score"), config.stall_start, 100,
        config.stall_max_penalty,
    )
    atr_penalty = _linear_penalty(
        _number(row, "ATR_ratio"), config.atr_start, config.atr_full_penalty,
        config.atr_max_penalty,
    )
    risk_penalty = exhaustion_penalty + position_penalty + stall_penalty + atr_penalty
    adjustment, market_score = market_adjustment(market_metrics, config)
    if pd.notna(score):
        score = _clip(score - risk_penalty + adjustment)

    ranked = sorted(
        ((name, value) for name, value in positive_contributions.items() if pd.notna(value)),
        key=lambda item: item[1], reverse=True,
    )
    strengths = "、".join(name for name, _ in ranked[:2]) or "有效信号不足"
    risks = []
    if exhaustion_penalty > 0:
        risks.append("高位衰竭")
    if position_penalty > 0:
        risks.append("位置偏高")
    if stall_penalty > 0:
        risks.append("趋势停滞")
    if atr_penalty > 0:
        risks.append("波动偏高")
    market_text = "大盘顺风" if adjustment >= 3 else ("大盘逆风" if adjustment <= -3 else "大盘中性")
    explanation = f"主要支撑：{strengths}；{market_text}"
    if risks:
        explanation += f"；风险：{'、'.join(risks)}"
    if coverage < 1:
        explanation += f"；信号覆盖{coverage:.0%}"

    return {
        "机会评分": round(score, 2) if pd.notna(score) else np.nan,
        "机会等级": opportunity_level(score, config),
        "趋势贡献": round(positive_contributions.get("趋势质量", np.nan), 2),
        "相对强弱贡献": round(positive_contributions.get("相对强弱", np.nan), 2),
        "突破贡献": round(positive_contributions.get("突破动能", np.nan), 2),
        "确认贡献": round(positive_contributions.get("趋势确认", np.nan), 2),
        "形态贡献": round(positive_contributions.get("形态准备", np.nan), 2),
        "风险扣分": round(risk_penalty, 2),
        "大盘调整": round(adjustment, 2),
        "大盘环境分": round(market_score, 2) if pd.notna(market_score) else np.nan,
        "信号覆盖率": round(coverage, 4),
        "机会评分说明": explanation,
        "机会评分版本": SCORE_VERSION,
        "评分验证状态": "实验性：尚未证明稳定排序能力",
    }


def add_opportunity_scores(
    frame: pd.DataFrame,
    *,
    market_metrics: Mapping[str, Any] | None = None,
    config: OpportunityConfig | None = None,
) -> pd.DataFrame:
    config = config or load_opportunity_config()
    scored = frame.copy()
    additions = scored.apply(
        lambda row: pd.Series(score_opportunity(
            row, market_metrics=market_metrics, config=config
        )),
        axis=1,
    )
    for column in additions.columns:
        scored[column] = additions[column]
    return scored


def is_index_row(row: Mapping[str, Any]) -> bool:
    code = normalize_code(row.get("代码", ""), "suffix")
    name = str(row.get("名称", ""))
    return code in KNOWN_INDEX_CODES or name in {
        "上证指数", "上证50", "沪深300", "科创50", "中证500", "深证成指", "创业板指"
    }


def opportunity_output(frame: pd.DataFrame) -> pd.DataFrame:
    """返回按机会评分降序排列、适合页面展示的非指数股票表。"""
    stocks = frame.loc[~frame.apply(is_index_row, axis=1)].copy()
    preferred = [
        "代码", "名称", "市场", "分类", "机会评分", "机会等级",
        "趋势贡献", "相对强弱贡献", "突破贡献", "确认贡献", "形态贡献",
        "风险扣分", "大盘调整", "信号覆盖率", "机会评分说明", "机会评分版本",
        "评分验证状态",
        "最新价", "涨跌幅", "5日涨跌幅", "20日涨跌幅", "60日涨跌幅",
        "所属行业", "市值",
    ]
    columns = [column for column in preferred if column in stocks.columns]
    return stocks[columns].sort_values(
        ["机会评分", "代码"], ascending=[False, True], na_position="last"
    ).reset_index(drop=True)
