"""日报大模型输出的结构化约束与确定性降级。"""

from __future__ import annotations

import re
from typing import Any


LIMITED_SECTIONS = {
    "turning_points": 4,
    "risks": 4,
}
DIGIT_RE = re.compile(r"\d")


def evidence_index(evidence: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["id"]: item
        for item in [*evidence.get("industries", []), *evidence.get("tags", [])]
    }


def validate_analysis(payload: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("模型输出必须是JSON对象")
    allowed = set(evidence_index(evidence))
    required = {
        "industry_view": {
            item["id"] for item in evidence.get("industries", []) if item.get("sample_count", 0) > 3
        },
        "tag_view": {
            item["id"] for item in evidence.get("tags", []) if item.get("sample_count", 0) > 3
        },
    }
    summary = str(payload.get("market_summary") or "").strip()
    if not summary or DIGIT_RE.search(summary):
        raise ValueError("市场总结不能为空，也不能自行包含数字")
    result: dict[str, Any] = {"market_summary": summary}
    for section in ("industry_view", "tag_view", "turning_points", "risks"):
        raw_items = payload.get(section) or []
        if not isinstance(raw_items, list):
            raise ValueError(f"{section}必须是数组")
        limit = LIMITED_SECTIONS.get(section)
        if limit is not None and len(raw_items) > limit:
            raise ValueError(f"{section}最多允许{limit}项")
        checked = []
        seen_ids: list[str] = []
        for item in raw_items:
            if not isinstance(item, dict):
                raise ValueError(f"{section}包含非对象项")
            ids = item.get("evidence_ids") or []
            if not isinstance(ids, list) or not ids or any(value not in allowed for value in ids):
                raise ValueError(f"{section}引用了未知或空证据编号")
            if section in required:
                if len(ids) != 1 or ids[0] not in required[section]:
                    raise ValueError(f"{section}必须逐项引用对应类型的合格证据")
                seen_ids.append(ids[0])
            interpretation = str(item.get("interpretation") or "").strip()
            caveat = str(item.get("caveat") or "").strip()
            if not interpretation or DIGIT_RE.search(interpretation) or DIGIT_RE.search(caveat):
                raise ValueError(f"{section}的文字不能为空或自行包含数字")
            checked.append(
                {
                    "evidence_ids": list(dict.fromkeys(ids))[:3],
                    "interpretation": interpretation,
                    "caveat": caveat,
                }
            )
        result[section] = checked
        if section in required and (set(seen_ids) != required[section] or len(seen_ids) != len(required[section])):
            missing = sorted(required[section] - set(seen_ids))
            raise ValueError(f"{section}未完整覆盖全部合格证据：{missing[:5]}")
    return result


def _item(ids: list[str], interpretation: str, caveat: str = "") -> dict[str, Any]:
    return {"evidence_ids": ids, "interpretation": interpretation, "caveat": caveat}


def group_interpretation(group: dict[str, Any], market: dict[str, Any], noun: str) -> tuple[str, str]:
    positive = group.get("positive_signals") or []
    negative = group.get("negative_signals") or []
    board = group.get("board") or "neutral"
    if board == "red":
        interpretation = f"{noun}进入红榜，正向证据主要来自{'、'.join(positive[:4])}。"
    elif board == "black":
        interpretation = f"{noun}进入黑榜，压力主要来自{'、'.join(negative[:4])}。"
    else:
        interpretation = f"{noun}多维信号尚未形成明确同向结论。"

    daily = group.get("daily_return_median")
    market_daily = market.get("daily_return_median")
    if daily is not None and market_daily is not None:
        if daily > 0 and market_daily < 0:
            interpretation += "当日表现逆势走强。"
        elif daily < 0 and daily > market_daily:
            interpretation += "当日虽有回落，但相对股票池更抗跌。"
        elif daily < market_daily:
            interpretation += "当日表现弱于股票池整体。"
    benchmark_name = group.get("benchmark_name")
    excess_daily = group.get("excess_daily_return")
    excess_20d = group.get("excess_20d_return")
    if benchmark_name and excess_daily is not None and excess_20d is not None:
        if excess_daily > 0 and excess_20d > 0:
            interpretation += "当日与中期均跑赢大盘基准。"
        elif excess_daily < 0 and excess_20d < 0:
            interpretation += "当日与中期均跑输大盘基准。"
        else:
            interpretation += "相对大盘基准的短中期表现存在分化。"
    medium_supported = (group.get("return_20d_median") or 0) > 0 and (group.get("strong_state_ratio") or 0) >= 0.5
    interpretation += "中期趋势提供支持。" if medium_supported else "中期趋势尚未形成充分支持。"

    caveats = []
    if group.get("sample_count") == 4:
        caveats.append("样本规模较小")
    if board == "red" and negative:
        caveats.append(f"仍存在{'、'.join(negative[:2])}")
    elif board == "black" and positive:
        caveats.append(f"仍有{'、'.join(positive[:2])}等正向证据")
    return interpretation, "；".join(caveats) + ("。" if caveats else "")


def deterministic_analysis(evidence: dict[str, Any]) -> dict[str, Any]:
    market = evidence["market"]
    up_ratio = market.get("up_ratio") or 0
    strong_ratio = market.get("strong_state_ratio") or 0
    primary = evidence.get("benchmarks", {}).get("primary") or {}
    benchmark_daily = primary.get("daily_return")
    if up_ratio >= 0.6 and strong_ratio >= 0.45:
        summary = "股票池整体偏强，短期涨幅与趋势结构形成一定共振。"
    elif up_ratio <= 0.4:
        summary = "股票池整体偏弱，行业与标签之间仍存在局部结构性机会。"
    else:
        summary = "股票池涨跌分化，强势方向主要集中在少数行业与产业标签。"
    if benchmark_daily is not None:
        summary += "大盘同步走强。" if benchmark_daily > 0 else "大盘同步承压。" if benchmark_daily < 0 else "大盘表现平稳。"

    rankings = evidence["rankings"]
    index = evidence_index(evidence)
    industry_view = []
    eligible_industries = sorted(
        (group for group in evidence.get("industries", []) if group.get("sample_count", 0) > 3),
        key=lambda group: group.get("daily_return_median") if group.get("daily_return_median") is not None else float("-inf"),
        reverse=True,
    )
    for group in eligible_industries:
        group_id = group["id"]
        interpretation, caveat = group_interpretation(group, market, "行业")
        industry_view.append(_item([group_id], interpretation, caveat))

    tag_view = []
    eligible_tags = sorted(
        (group for group in evidence.get("tags", []) if group.get("sample_count", 0) > 3),
        key=lambda group: group.get("daily_return_median") if group.get("daily_return_median") is not None else float("-inf"),
        reverse=True,
    )
    for group in eligible_tags:
        group_id = group["id"]
        interpretation, caveat = group_interpretation(group, market, "标签")
        tag_view.append(_item([group_id], interpretation, caveat))

    turning_points = []
    candidates = [
        group for group in index.values()
        if group["sample_count"] > 3
        and (group.get("became_strong_count", 0) > 0 or group.get("became_weak_count", 0) > 0)
    ]
    candidates.sort(
        key=lambda group: abs(group.get("became_strong_count", 0) - group.get("became_weak_count", 0)),
        reverse=True,
    )
    seen_names: set[str] = set()
    for group in candidates:
        if group["name"] in seen_names:
            continue
        seen_names.add(group["name"])
        improving = group.get("became_strong_count", 0) > group.get("became_weak_count", 0)
        turning_points.append(
            _item(
                [group["id"]],
                "内部分类转强迹象更明显。" if improving else "内部分类转弱迹象更明显。",
                "分类变化需要后续交易日确认。",
            )
        )
        if len(turning_points) == 4:
            break

    risks = []
    risk_ids = list(dict.fromkeys([
        *rankings.get("industry_high_exhaustion", [])[:2],
        *rankings.get("tag_high_exhaustion", [])[:2],
    ]))
    for group_id in risk_ids[:4]:
        if (index[group_id].get("exhaustion_score_median") or 0) < 70:
            continue
        risks.append(_item([group_id], "价格衰竭水平相对靠前，需要防范高位波动。", "强趋势不等于低风险。"))

    return validate_analysis(
        {
            "market_summary": summary,
            "industry_view": industry_view,
            "tag_view": tag_view,
            "turning_points": turning_points,
            "risks": risks,
        },
        evidence,
    )
