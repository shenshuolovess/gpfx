"""为行业与产业标签日报生成可审计、确定性的统计证据。"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from pipeline_config import PROJECT_DIR, resolve_input
from stock_utils import normalize_code, read_csv_auto


DATE_RE = re.compile(r"(20\d{6})")
STRONG_STATES = {"上升", "震荡上行"}
WEAK_STATES = {"下降", "震荡下行"}
NON_STOCK_CODES = {
    "000001.SH", "000016.SH", "000300.SH", "000688.SH", "000905.SH",
    "399001.SZ", "399006.SZ",
}
NON_STOCK_NAMES = {"上证50", "沪深300", "中证500", "科创50", "深证成指", "创业板指"}
PRIMARY_BENCHMARK_CODE = "000300.SH"


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if bool(pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


def number(value: Any) -> float:
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return math.nan


def finite(value: Any, digits: int = 2) -> float | None:
    value = number(value)
    return None if math.isnan(value) else round(value, digits)


def date_from_name(path: Path) -> str:
    match = DATE_RE.search(path.stem)
    return match.group(1) if match else ""


def dated_candidates(pattern: str, *, exclude: str = "") -> list[Path]:
    paths = [path for path in PROJECT_DIR.glob(pattern) if not exclude or exclude not in path.name]
    return sorted(paths, key=lambda path: (date_from_name(path), path.stat().st_mtime_ns))


def choose_dated_file(pattern: str, date_tag: str | None = None, *, exclude: str = "") -> Path:
    paths = dated_candidates(pattern, exclude=exclude)
    if date_tag:
        paths = [path for path in paths if date_from_name(path) == date_tag]
    if not paths:
        suffix = f"，日期 {date_tag}" if date_tag else ""
        raise FileNotFoundError(f"找不到文件：{pattern}{suffix}")
    return paths[-1]


def previous_file(current: Path, pattern: str) -> Path | None:
    current_date = date_from_name(current)
    earlier = [path for path in dated_candidates(pattern) if date_from_name(path) < current_date]
    return earlier[-1] if earlier else None


def previous_signal_snapshot(current_date: str) -> Path | None:
    paths = [
        path
        for path in dated_candidates("data/history/snapshots/signals/*.csv")
        if date_from_name(path) < current_date
    ]
    return paths[-1] if paths else None


def is_non_stock(code: Any, name: Any) -> bool:
    normalized = normalize_code(code, "suffix")
    text = clean_text(name)
    return normalized in NON_STOCK_CODES or text.endswith("指数") or text in NON_STOCK_NAMES


def latest_financial_rows(path: Path | None) -> pd.DataFrame:
    columns = ["代码", "营收同比", "净利同比", "ROIC", "资产负债率"]
    if path is None:
        return pd.DataFrame(columns=columns)
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for code, record in payload.get("records", {}).items():
        financials = record.get("financials") or []
        latest = max(financials, key=lambda item: str(item.get("REPORT_DATE") or ""), default={})
        rows.append(
            {
                "代码": normalize_code(code, "suffix"),
                "营收同比": finite(latest.get("TOTALOPERATEREVETZ")),
                "净利同比": finite(latest.get("PARENTNETPROFITTZ")),
                "ROIC": finite(latest.get("ROIC")),
                "资产负债率": finite(latest.get("ZCFZL")),
            }
        )
    return pd.DataFrame(rows, columns=columns)


def prepare_stock_frame(
    classification: pd.DataFrame,
    tags: pd.DataFrame,
    financials: pd.DataFrame,
    previous: pd.DataFrame | None = None,
) -> pd.DataFrame:
    frame = classification.copy()
    frame["代码"] = frame["代码"].map(lambda value: normalize_code(value, "suffix"))
    frame = frame.loc[
        ~frame.apply(lambda row: is_non_stock(row.get("代码"), row.get("名称")), axis=1)
    ].copy()
    tag_frame = tags.copy()
    tag_frame["代码"] = tag_frame["代码"].map(lambda value: normalize_code(value, "suffix"))
    tag_columns = ["代码"] + [
        column for column in tag_frame.columns if column.startswith("标签") and column != "标签状态"
    ]
    frame = frame.merge(tag_frame[tag_columns], on="代码", how="left", suffixes=("", "_标签"))
    if not financials.empty:
        frame = frame.merge(financials, on="代码", how="left")
    if previous is not None and not previous.empty:
        prior = previous[["代码", "分类"]].copy()
        prior["代码"] = prior["代码"].map(lambda value: normalize_code(value, "suffix"))
        frame = frame.merge(prior.rename(columns={"分类": "前日分类"}), on="代码", how="left")
    else:
        frame["前日分类"] = ""

    numeric_columns = {
        "涨跌幅": "今日涨跌幅",
        "5日涨跌幅": "5日表现",
        "20日涨跌幅": "20日表现",
        "60日涨跌幅": "60日表现",
        "trend_score": "趋势强度",
        "rs_score": "相对强弱",
        "position_score": "价格位置",
        "exhaustion_score": "衰竭风险",
        "市盈率TTM": "市盈率TTM数值",
        "市净率": "市净率数值",
        "市值": "总市值数值",
    }
    for source, target in numeric_columns.items():
        frame[target] = frame.get(source, pd.Series(index=frame.index, dtype=object)).map(number)
    frame["分类"] = frame.get("分类", "").map(clean_text)
    frame["前日分类"] = frame.get("前日分类", "").map(clean_text)
    frame["所属行业"] = frame.get("所属行业", "").map(clean_text).replace("", "未标注行业")
    frame["当日上涨"] = frame["今日涨跌幅"] > 0
    frame["强势分类"] = frame["分类"].isin(STRONG_STATES)
    frame["弱势分类"] = frame["分类"].isin(WEAK_STATES)
    has_previous = frame["前日分类"].ne("")
    frame["转强"] = has_previous & frame["强势分类"] & ~frame["前日分类"].isin(STRONG_STATES)
    frame["转弱"] = has_previous & frame["弱势分类"] & ~frame["前日分类"].isin(WEAK_STATES)
    return frame


def benchmark_summary(classification: pd.DataFrame) -> dict[str, Any]:
    """从分类总表提取指数行情，沪深300作为行业和标签的统一比较基准。"""
    rows = []
    for _, row in classification.iterrows():
        code = normalize_code(row.get("代码"), "suffix")
        name = clean_text(row.get("名称"))
        if not is_non_stock(code, name):
            continue
        rows.append(
            {
                "code": code,
                "name": name,
                "classification": clean_text(row.get("分类")),
                "daily_return": finite(row.get("涨跌幅")),
                "return_5d": finite(row.get("5日涨跌幅")),
                "return_20d": finite(row.get("20日涨跌幅")),
                "return_60d": finite(row.get("60日涨跌幅")),
                "trend_score": finite(row.get("trend_score")),
            }
        )
    primary = next((row for row in rows if row["code"] == PRIMARY_BENCHMARK_CODE), None)
    if primary is None:
        primary = next((row for row in rows if row["code"] == "000001.SH"), None)
    return {
        "primary_code": primary.get("code", "") if primary else "",
        "primary_name": primary.get("name", "") if primary else "",
        "primary": primary or {},
        "indices": rows,
    }


def attach_benchmark_relative(group: dict[str, Any], benchmark: dict[str, Any]) -> None:
    primary = benchmark.get("primary") or {}
    group["benchmark_name"] = primary.get("name") or ""
    for group_field, index_field, output_field in (
        ("daily_return_median", "daily_return", "excess_daily_return"),
        ("return_5d_median", "return_5d", "excess_5d_return"),
        ("return_20d_median", "return_20d", "excess_20d_return"),
        ("return_60d_median", "return_60d", "excess_60d_return"),
    ):
        group_value = group.get(group_field)
        index_value = primary.get(index_field)
        group[output_field] = (
            round(group_value - index_value, 2)
            if group_value is not None and index_value is not None
            else None
        )


def median(series: pd.Series) -> float | None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    return None if values.empty else round(float(values.median()), 2)


def ratio(series: pd.Series) -> float | None:
    values = series.dropna()
    return None if values.empty else round(float(values.astype(bool).mean()), 4)


def representative_rows(group: pd.DataFrame, ascending: bool, limit: int) -> list[dict[str, Any]]:
    selected = group.dropna(subset=["今日涨跌幅"]).sort_values("今日涨跌幅", ascending=ascending).head(limit)
    return [
        {
            "code": clean_text(row.get("代码")),
            "name": clean_text(row.get("名称")),
            "daily_return": finite(row.get("今日涨跌幅")),
            "classification": clean_text(row.get("分类")),
        }
        for _, row in selected.iterrows()
    ]


def main_stock_rows(group: pd.DataFrame, kind: str, limit: int = 8) -> list[dict[str, Any]]:
    ranked = group.copy()
    if kind == "tag":
        ranked = ranked.sort_values(
            ["标签相关度", "总市值数值"], ascending=[False, False], na_position="last"
        )
    else:
        ranked = ranked.sort_values("总市值数值", ascending=False, na_position="last")
    ranked = ranked.drop_duplicates("代码").head(limit)
    return [
        {
            "code": clean_text(row.get("代码")),
            "name": clean_text(row.get("名称")),
            "market_cap": finite(row.get("总市值数值"), 0),
            "daily_return": finite(row.get("今日涨跌幅")),
            "classification": clean_text(row.get("分类")),
            "relevance": finite(row.get("标签相关度"), 0) if kind == "tag" else None,
        }
        for _, row in ranked.iterrows()
    ]


def financial_coverage(group: pd.DataFrame) -> tuple[int, float | None]:
    members = group.drop_duplicates("代码")
    columns = [column for column in ("营收同比", "净利同比", "ROIC") if column in members.columns]
    if not columns:
        return 0, 0.0 if len(members) else None
    covered = members[columns].apply(
        lambda row: any(not math.isnan(number(value)) for value in row), axis=1
    )
    count = int(covered.sum())
    return count, round(count / len(members), 4) if len(members) else None


def single_stock_influence(group: pd.DataFrame) -> dict[str, Any]:
    """留一法检查单只股票是否显著改变组内核心中位数和分类占比。"""
    members = group.drop_duplicates("代码").copy()
    if len(members) < 2:
        return {"is_high": True, "stock_code": "", "stock_name": "", "reasons": ["样本不足"]}
    baseline = {
        "daily": median(members["今日涨跌幅"]),
        "return_20d": median(members["20日表现"]),
        "strong_ratio": ratio(members["强势分类"]),
    }
    candidates = []
    for index, row in members.iterrows():
        remaining = members.drop(index=index)
        changes = {
            "daily_median_change": abs((median(remaining["今日涨跌幅"]) or 0) - (baseline["daily"] or 0)),
            "return_20d_median_change": abs((median(remaining["20日表现"]) or 0) - (baseline["return_20d"] or 0)),
            "strong_ratio_change": abs((ratio(remaining["强势分类"]) or 0) - (baseline["strong_ratio"] or 0)),
        }
        severity = max(
            changes["daily_median_change"] / 1.5,
            changes["return_20d_median_change"] / 5.0,
            changes["strong_ratio_change"] / 0.15,
        )
        candidates.append((severity, row, changes))
    severity, row, changes = max(candidates, key=lambda item: item[0])
    reasons = []
    if changes["daily_median_change"] >= 1.5:
        reasons.append(f"当日中位变化{changes['daily_median_change']:.2f}个百分点")
    if changes["return_20d_median_change"] >= 5:
        reasons.append(f"二十日中位变化{changes['return_20d_median_change']:.2f}个百分点")
    if changes["strong_ratio_change"] >= 0.15:
        reasons.append(f"强势分类占比变化{changes['strong_ratio_change'] * 100:.1f}个百分点")
    return {
        "is_high": severity >= 1,
        "stock_code": clean_text(row.get("代码")),
        "stock_name": clean_text(row.get("名称")),
        "reasons": reasons,
        **{key: round(value, 4) for key, value in changes.items()},
    }


def summarize_group(group: pd.DataFrame, group_id: str, name: str, kind: str) -> dict[str, Any]:
    classification_counts = {
        key: int(value)
        for key, value in group["分类"].value_counts().items()
        if clean_text(key)
    }
    financial_count, financial_ratio = financial_coverage(group)
    report = {
        "id": f"{kind}:{name}",
        "name": name,
        "kind": kind,
        "sample_count": int(group["代码"].nunique()),
        "member_codes": sorted(group["代码"].dropna().astype(str).unique().tolist()),
        "daily_return_median": median(group["今日涨跌幅"]),
        "up_ratio": ratio(group["当日上涨"]),
        "return_5d_median": median(group["5日表现"]),
        "return_20d_median": median(group["20日表现"]),
        "return_60d_median": median(group["60日表现"]),
        "trend_score_median": median(group["趋势强度"]),
        "rs_score_median": median(group["相对强弱"]),
        "position_score_median": median(group["价格位置"]),
        "exhaustion_score_median": median(group["衰竭风险"]),
        "strong_state_ratio": ratio(group["强势分类"]),
        "weak_state_ratio": ratio(group["弱势分类"]),
        "became_strong_count": int(group["转强"].sum()),
        "became_weak_count": int(group["转弱"].sum()),
        "pe_median": median(group["市盈率TTM数值"].where(group["市盈率TTM数值"] > 0)),
        "pb_median": median(group["市净率数值"].where(group["市净率数值"] > 0)),
        "revenue_yoy_median": median(group.get("营收同比", pd.Series(dtype=float))),
        "profit_yoy_median": median(group.get("净利同比", pd.Series(dtype=float))),
        "roic_median": median(group.get("ROIC", pd.Series(dtype=float))),
        "financial_coverage_count": financial_count,
        "financial_coverage_ratio": financial_ratio,
        "single_stock_influence": single_stock_influence(group),
        "classification_counts": classification_counts,
        "leaders": representative_rows(group, ascending=False, limit=3),
        "laggards": representative_rows(group, ascending=True, limit=2),
        "main_stocks": main_stock_rows(group, kind),
    }
    if kind == "tag":
        report["average_relevance"] = median(group["标签相关度"])
    return report


def industry_groups(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        summarize_group(group, name, name, "industry")
        for name, group in frame.groupby("所属行业", sort=True)
    ]


def explode_tags(frame: pd.DataFrame) -> pd.DataFrame:
    records = []
    for _, row in frame.iterrows():
        seen: set[str] = set()
        for index in range(1, 4):
            tag = clean_text(row.get(f"标签{index}"))
            if not tag or tag in seen:
                continue
            seen.add(tag)
            item = row.to_dict()
            item["产业标签"] = tag
            item["标签相关度"] = number(row.get(f"标签{index}相关度"))
            records.append(item)
    return pd.DataFrame(records)


def tag_groups(frame: pd.DataFrame) -> list[dict[str, Any]]:
    exploded = explode_tags(frame)
    if exploded.empty:
        return []
    return [
        summarize_group(group, name, name, "tag")
        for name, group in exploded.groupby("产业标签", sort=True)
    ]


def rank_ids(groups: list[dict[str, Any]], field: str, *, reverse: bool = True, limit: int = 8) -> list[str]:
    eligible = [group for group in groups if group["sample_count"] > 3 and group.get(field) is not None]
    eligible.sort(key=lambda group: group[field], reverse=reverse)
    result = []
    seen_members: set[tuple[str, ...]] = set()
    for group in eligible:
        members = tuple(group.get("member_codes") or [group["id"]])
        if members in seen_members:
            continue
        seen_members.add(members)
        result.append(group["id"])
        if len(result) == limit:
            break
    return result


def build_rankings(industries: list[dict[str, Any]], tags: list[dict[str, Any]]) -> dict[str, list[str]]:
    rankings = {
        "industry_daily_leaders": rank_ids(industries, "daily_return_median"),
        "industry_daily_laggards": rank_ids(industries, "daily_return_median", reverse=False),
        "industry_20d_leaders": rank_ids(industries, "return_20d_median"),
        "industry_high_exhaustion": rank_ids(industries, "exhaustion_score_median"),
        "tag_daily_leaders": rank_ids(tags, "daily_return_median"),
        "tag_daily_laggards": rank_ids(tags, "daily_return_median", reverse=False),
        "tag_20d_leaders": rank_ids(tags, "return_20d_median"),
        "tag_high_exhaustion": rank_ids(tags, "exhaustion_score_median"),
    }
    for kind, groups in (("industry", industries), ("tag", tags)):
        red = [group for group in groups if group.get("sample_count", 0) > 3 and group.get("board") == "red"]
        black = [group for group in groups if group.get("sample_count", 0) > 3 and group.get("board") == "black"]
        red.sort(key=lambda group: (group.get("signal_balance", 0), group.get("daily_return_median") or -999), reverse=True)
        black.sort(key=lambda group: (group.get("signal_balance", 0), group.get("daily_return_median") or 999))
        rankings[f"{kind}_red_board"] = [group["id"] for group in red]
        rankings[f"{kind}_black_board"] = [group["id"] for group in black]
    return rankings


def assess_group(group: dict[str, Any], market: dict[str, Any]) -> None:
    """用可解释的条件计数形成红黑榜，不把计数伪装成收益预测。"""
    positive: list[str] = []
    negative: list[str] = []
    daily = group.get("daily_return_median")
    market_daily = market.get("daily_return_median")
    up_ratio = group.get("up_ratio")
    return_20d = group.get("return_20d_median")
    return_60d = group.get("return_60d_median")
    trend = group.get("trend_score_median")
    rs = group.get("rs_score_median")
    position = group.get("position_score_median")
    exhaustion = group.get("exhaustion_score_median")
    strong_ratio = group.get("strong_state_ratio")
    revenue_yoy = group.get("revenue_yoy_median")
    profit_yoy = group.get("profit_yoy_median")
    roic = group.get("roic_median")
    pe = group.get("pe_median")
    excess_daily = group.get("excess_daily_return")
    excess_20d = group.get("excess_20d_return")

    if daily is not None and market_daily is not None:
        if daily >= market_daily + 1:
            positive.append("相对股票池抗跌或更强")
        elif daily <= market_daily - 1:
            negative.append("当日弱于股票池")
    if excess_daily is not None and excess_20d is not None:
        if excess_daily >= 1 and excess_20d >= 3:
            positive.append("短中期跑赢大盘基准")
        elif excess_daily <= -1 and excess_20d <= -3:
            negative.append("短中期跑输大盘基准")
    if up_ratio is not None:
        if up_ratio >= 0.6:
            positive.append("上涨覆盖面较广")
        elif up_ratio <= 0.25:
            negative.append("下跌覆盖面较广")
    if return_20d is not None and return_60d is not None:
        if return_20d > 0 and return_60d > 0:
            positive.append("中期表现保持正向")
        elif return_20d < 0 and return_60d < 0:
            negative.append("中期表现持续走弱")
    if strong_ratio is not None and trend is not None:
        if strong_ratio >= 0.5 and trend >= 60:
            positive.append("趋势结构偏强")
        elif strong_ratio <= 0.25 and trend < 50:
            negative.append("趋势结构偏弱")
    if rs is not None:
        if rs >= 60:
            positive.append("相对强弱较高")
        elif rs <= 40:
            negative.append("相对强弱较低")
    became_strong = group.get("became_strong_count", 0)
    became_weak = group.get("became_weak_count", 0)
    if became_strong > became_weak and became_strong > 0:
        positive.append("分类净转强")
    elif became_weak > became_strong and became_weak > 0:
        negative.append("分类净转弱")
    if revenue_yoy is not None and profit_yoy is not None:
        if revenue_yoy > 0 and profit_yoy > 0:
            positive.append("营收与利润同步增长")
        elif revenue_yoy < 0 and profit_yoy < 0:
            negative.append("营收与利润同步承压")
    if roic is not None and roic >= 8:
        positive.append("资本回报具备支撑")
    if exhaustion is not None and position is not None and exhaustion >= 70 and position >= 65:
        negative.append("高位衰竭风险偏高")
    if pe is not None and pe >= 100 and profit_yoy is not None and profit_yoy <= 0:
        negative.append("高估值叠加利润压力")

    balance = len(positive) - len(negative)
    red_anchor_signals = {
        "上涨覆盖面较广", "中期表现保持正向", "趋势结构偏强", "相对强弱较高"
    }
    has_red_anchor = bool(red_anchor_signals.intersection(positive))
    if len(positive) >= 3 and balance >= 2 and has_red_anchor:
        board = "red"
    elif len(negative) >= 3 and balance <= -2:
        board = "black"
    else:
        board = "neutral"
    group["positive_signals"] = positive
    group["negative_signals"] = negative
    group["positive_signal_count"] = len(positive)
    group["negative_signal_count"] = len(negative)
    group["signal_balance"] = balance
    group["board"] = board


def global_summary(frame: pd.DataFrame) -> dict[str, Any]:
    return {
        "stock_count": int(frame["代码"].nunique()),
        "daily_return_median": median(frame["今日涨跌幅"]),
        "up_count": int((frame["今日涨跌幅"] > 0).sum()),
        "down_count": int((frame["今日涨跌幅"] < 0).sum()),
        "flat_count": int((frame["今日涨跌幅"] == 0).sum()),
        "up_ratio": ratio(frame["当日上涨"]),
        "strong_state_ratio": ratio(frame["强势分类"]),
        "weak_state_ratio": ratio(frame["弱势分类"]),
        "became_strong_count": int(frame["转强"].sum()),
        "became_weak_count": int(frame["转弱"].sum()),
        "classification_counts": {
            key: int(value) for key, value in frame["分类"].value_counts().items() if clean_text(key)
        },
    }


def build_evidence(date_tag: str | None = None) -> dict[str, Any]:
    pool_path = resolve_input(None, config_key="stock_pool")
    classification_path = choose_dated_file("data/output/沪深_分类总表_*.csv", date_tag)
    resolved_date = date_from_name(classification_path) or datetime.now().strftime("%Y%m%d")
    tag_path = choose_dated_file("data/output/沪深_产业标签_*.csv", resolved_date, exclude="审计")
    previous_path = previous_file(classification_path, "data/output/沪深_分类总表_*.csv")
    if previous_path is None:
        previous_path = previous_signal_snapshot(resolved_date)
    financial_candidates = dated_candidates("data/history/company_financials/eastmoney_company_financials_*.json")
    financial_path = financial_candidates[-1] if financial_candidates else None

    classification = read_csv_auto(classification_path, dtype=str)
    tags = read_csv_auto(tag_path, dtype=str)
    previous = read_csv_auto(previous_path, dtype=str) if previous_path else None
    financials = latest_financial_rows(financial_path)
    frame = prepare_stock_frame(classification, tags, financials, previous)
    market = global_summary(frame)
    benchmarks = benchmark_summary(classification)
    industries = industry_groups(frame)
    tags_summary = tag_groups(frame)
    for group in [*industries, *tags_summary]:
        attach_benchmark_relative(group, benchmarks)
        assess_group(group, market)
    evidence = {
        "schema_version": 1,
        "as_of": resolved_date,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scope_note": "统计范围仅为当日沪深.csv股票池，不代表沪深全市场；标签组可相互重叠。",
        "sources": {
            "stock_pool": str(pool_path),
            "classification": str(classification_path),
            "previous_classification": str(previous_path) if previous_path else "",
            "tags": str(tag_path),
            "financials": str(financial_path) if financial_path else "",
        },
        "market": market,
        "benchmarks": benchmarks,
        "industries": industries,
        "tags": tags_summary,
        "rankings": build_rankings(industries, tags_summary),
    }
    return evidence
