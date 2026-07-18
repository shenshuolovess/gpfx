"""趋势分类规则。保持纯函数，便于边界测试和历史回测。"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any, Mapping

import pandas as pd


REQUIRED_CLASSIFICATION_FIELDS = (
    "trend_score",
    "direction_score",
    "trend_stability_score",
    "adx_score",
    "position_score",
    "rs_score",
    "breakout_score",
    "base_score",
    "exhaustion_score",
    "ma_structure_score",
    "stabilize_score",
    "R20",
    "RS20",
    "MA20",
    "close",
)


@dataclass(frozen=True)
class RuleConfig:
    """分类阈值集合；默认值就是当前生产规则。"""

    stabilize_min: float = 58
    stabilized_r20_min: float = -0.02
    stabilized_rs20_min: float = -0.06

    top_position_min: float = 88
    top_exhaustion_min: float = 82
    top_trend_min: float = 65
    top_direction_strict_min: float = 20

    base_position_max: float = 35
    base_score_min: float = 68
    base_adx_max: float = 50
    base_direction_strict_min: float = -45

    rising_trend_min: float = 72
    rising_direction_min: float = 28
    rising_adx_min: float = 55
    rising_rs_min: float = 15
    rising_breakout_min: float = 60
    rising_ma_structure_min: float = 50
    rising_exhaustion_max_exclusive: float = 88

    falling_trend_max: float = 32
    falling_direction_max: float = -28
    falling_adx_min: float = 50
    falling_rs_max: float = -15
    falling_breakout_max: float = -60
    falling_ma_structure_max: float = -50

    oscillating_up_trend_min: float = 52
    oscillating_up_trend_max_exclusive: float = 72
    oscillating_up_direction_min: float = 10
    oscillating_up_rs_min: float = 0
    oscillating_up_breakout_strict_min: float = -20

    oscillating_down_trend_strict_min: float = 30
    oscillating_down_trend_max: float = 48
    oscillating_down_direction_max: float = -10
    oscillating_down_rs_max: float = 5

    sideways_trend_min: float = 40
    sideways_trend_max: float = 58
    sideways_direction_abs_exclusive: float = 18
    sideways_adx_max: float = 45
    sideways_breakout_abs_max: float = 20
    sideways_stability_max_exclusive: float = 55

    transition_trend_min: float = 35
    transition_trend_max: float = 68
    transition_direction_abs_min: float = 18
    transition_breakout_abs_min: float = 20
    transition_rs_abs_min: float = 15


CURRENT_RULES = RuleConfig()


def rule_config_from_mapping(
    overrides: Mapping[str, Any], *, base: RuleConfig = CURRENT_RULES
) -> RuleConfig:
    """用少量覆盖值构造候选规则，并拒绝拼错的阈值名。"""
    valid = {field.name for field in fields(RuleConfig)}
    unknown = sorted(set(overrides) - valid)
    if unknown:
        raise KeyError(f"未知分类阈值：{unknown}")
    values = {key: float(value) for key, value in overrides.items()}
    return replace(base, **values)


def classify_label(last_row: pd.Series, config: RuleConfig = CURRENT_RULES) -> str:
    """根据当日评分和技术指标返回趋势分类。"""
    if any(pd.isna(last_row.get(field)) for field in REQUIRED_CLASSIFICATION_FIELDS):
        return "边界模糊"

    trend_score = float(last_row["trend_score"])
    direction_score = float(last_row["direction_score"])
    trend_stability = float(last_row["trend_stability_score"])
    adx_score = float(last_row["adx_score"])
    position_score = float(last_row["position_score"])
    rs_score = float(last_row["rs_score"])
    breakout_score = float(last_row["breakout_score"])
    base_score = float(last_row["base_score"])
    exhaustion_score = float(last_row["exhaustion_score"])
    ma_structure_score = float(last_row["ma_structure_score"])
    stabilize_score = float(last_row["stabilize_score"])
    r20 = float(last_row["R20"])
    rs20 = float(last_row["RS20"])
    close = float(last_row["close"])
    ma20 = float(last_row["MA20"])

    short_stabilized = (
        stabilize_score >= config.stabilize_min
        and close > ma20
        and r20 > config.stabilized_r20_min
        and rs20 > config.stabilized_rs20_min
    )

    if (
        position_score >= config.top_position_min
        and exhaustion_score >= config.top_exhaustion_min
        and trend_score >= config.top_trend_min
        and direction_score > config.top_direction_strict_min
    ):
        return "赶顶"

    if (
        position_score <= config.base_position_max
        and base_score >= config.base_score_min
        and adx_score <= config.base_adx_max
        and direction_score > config.base_direction_strict_min
        and short_stabilized
    ):
        return "筑底"

    if (
        trend_score >= config.rising_trend_min
        and direction_score >= config.rising_direction_min
        and adx_score >= config.rising_adx_min
        and rs_score >= config.rising_rs_min
        and (
            breakout_score >= config.rising_breakout_min
            or ma_structure_score >= config.rising_ma_structure_min
        )
        and exhaustion_score < config.rising_exhaustion_max_exclusive
    ):
        return "上升"

    if (
        trend_score <= config.falling_trend_max
        and direction_score <= config.falling_direction_max
        and adx_score >= config.falling_adx_min
        and rs_score <= config.falling_rs_max
        and (
            breakout_score <= config.falling_breakout_max
            or ma_structure_score <= config.falling_ma_structure_max
        )
    ):
        return "下降"

    if (
        config.oscillating_up_trend_min
        <= trend_score
        < config.oscillating_up_trend_max_exclusive
        and direction_score >= config.oscillating_up_direction_min
        and rs_score >= config.oscillating_up_rs_min
        and breakout_score > config.oscillating_up_breakout_strict_min
    ):
        return "震荡上行"

    if (
        config.oscillating_down_trend_strict_min
        < trend_score
        <= config.oscillating_down_trend_max
        and direction_score <= config.oscillating_down_direction_max
        and rs_score <= config.oscillating_down_rs_max
    ):
        return "震荡下行"

    if (
        config.sideways_trend_min <= trend_score <= config.sideways_trend_max
        and abs(direction_score) < config.sideways_direction_abs_exclusive
        and adx_score <= config.sideways_adx_max
        and abs(breakout_score) <= config.sideways_breakout_abs_max
        and trend_stability < config.sideways_stability_max_exclusive
    ):
        return "横盘"

    if (
        config.transition_trend_min <= trend_score <= config.transition_trend_max
        and (
            abs(direction_score) >= config.transition_direction_abs_min
            or abs(breakout_score) >= config.transition_breakout_abs_min
            or abs(rs_score) >= config.transition_rs_abs_min
        )
    ):
        return "过渡状态"

    return "边界模糊"
