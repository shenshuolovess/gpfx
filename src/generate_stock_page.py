"""生成单只股票的离线研究图示页面。"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from history_store import load_history
from pipeline_config import PROJECT_DIR, config_value, project_path, resolve_input
from stock_utils import latest_matching_file, normalize_code, read_csv_auto


INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
LEGACY_CODE_FILENAME_RE = re.compile(r"\d{6}\.(?:SH|SZ|BJ)\.html", re.IGNORECASE)
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
KNOWN_INDEX_CODES = {
    "000001.SH", "000016.SH", "000300.SH", "000688.SH", "000905.SH",
    "399001.SZ", "399006.SZ",
}
GENERIC_RESEARCH_HEADINGS = {
    "事件", "投资要点", "经营分析", "投资建议", "盈利预测", "估值与评级",
    "盈利预测、估值与评级", "公司简介", "报告摘要", "核心观点", "主要观点",
}


def latest_industry_tag_file() -> Path:
    candidates = [
        path
        for path in (PROJECT_DIR / "data" / "output").glob("沪深_产业标签_*.csv")
        if "审计" not in path.name
    ]
    if not candidates:
        raise FileNotFoundError("未找到产业标签文件，请先运行 stock_industry_tags.py")
    return max(candidates, key=lambda path: (path.stem.split("_")[-1], path.stat().st_mtime_ns))


def latest_company_profile_file() -> Path:
    return latest_matching_file(
        PROJECT_DIR,
        "data/history/company_profiles/eastmoney_corethemes_*.json",
    )


def latest_company_financial_file() -> Path | None:
    candidates = list(
        (PROJECT_DIR / "data" / "history" / "company_financials").glob(
            "eastmoney_company_financials_*.json"
        )
    )
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def latest_research_report_file() -> Path | None:
    candidates = list(
        (PROJECT_DIR / "data" / "history" / "research_reports").glob(
            "eastmoney_stock_reports_*.json"
        )
    )
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def find_stock_row(frame: pd.DataFrame, code: str, label: str) -> pd.Series:
    target = normalize_code(code, "suffix")
    normalized = frame["代码"].map(lambda value: normalize_code(value, "suffix"))
    matches = frame.loc[normalized == target]
    if matches.empty:
        raise KeyError(f"{label}中找不到股票：{target}")
    return matches.iloc[0]


def numeric(value: Any, default: float = math.nan) -> float:
    try:
        text = str(value).replace(",", "").replace("%", "").strip()
        return float(text)
    except (TypeError, ValueError):
        return default


def scalar_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if bool(pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


def report_viewpoints(report: dict[str, Any]) -> list[str]:
    points = [
        scalar_text(item) for item in report.get("viewpoints", [])
        if scalar_text(item).strip("：: ") not in GENERIC_RESEARCH_HEADINGS
    ]
    return points[:3] or [scalar_text(report.get("title")) or "暂无可提取的观点标题"]


RESEARCH_FOCUS_RULES = {
    "业绩增长": ("业绩", "营收", "利润", "高增", "增长", "超预期"),
    "产品与产能": ("产品", "新品", "产能", "扩产", "放量", "投产"),
    "订单与交付": ("订单", "交付", "中标", "在手", "客户"),
    "AI与算力": ("AI", "算力", "服务器", "数据中心", "交换机", "CPO"),
    "海外业务": ("海外", "出海", "出口", "全球"),
    "价格与成本": ("价格", "成本", "毛利", "降本", "原材料"),
    "并购与整合": ("收购", "并购", "整合", "股权"),
}
RESEARCH_RISK_RULES = {
    "需求不及预期": ("需求不及预期", "需求下降", "需求疲软"),
    "行业竞争": ("竞争加剧", "行业竞争"),
    "价格与成本": ("价格下降", "价格波动", "原材料", "成本上升"),
    "项目与产能": ("项目不及预期", "产能不及预期", "投产不及预期", "交付不及预期"),
    "技术与研发": ("研发不及预期", "技术迭代", "技术风险"),
    "政策与宏观": ("政策", "宏观经济", "宏观环境"),
    "客户与供应链": ("客户", "供应链", "供应商"),
    "汇率与海外": ("汇率", "贸易摩擦", "海外市场"),
}


def _keyword_counts(reports: list[dict[str, Any]], rules: dict[str, tuple[str, ...]], field: str) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for report in reports:
        if field == "focus":
            text = " ".join([scalar_text(report.get("title")), *report_viewpoints(report)])
        else:
            text = scalar_text(report.get("risk"))
        for name, keywords in rules.items():
            if any(keyword.lower() in text.lower() for keyword in keywords):
                counts[name] += 1
    return [{"name": name, "count": count} for name, count in counts.most_common(4)]


def build_research_comparison(reports: list[dict[str, Any]], as_of: str | None = None) -> dict[str, Any]:
    selected = list(reports or [])[:3]
    if not selected:
        return {}
    ratings = [scalar_text(item.get("rating")) or "未评级" for item in selected]
    rating_counts = Counter(ratings)
    rated = [item for item in ratings if item != "未评级"]
    rating_consensus = "评级一致" if rated and len(set(rated)) == 1 else "评级存在分歧" if len(set(rated)) > 1 else "有效评级不足"
    latest_date = max((scalar_text(item.get("publish_date")) for item in selected), default="")
    age_days = None
    try:
        end = datetime.strptime(as_of or datetime.now().strftime("%Y-%m-%d"), "%Y-%m-%d")
        age_days = max(0, (end - datetime.strptime(latest_date, "%Y-%m-%d")).days)
    except ValueError:
        pass

    forecasts_by_year: dict[int, list[float]] = {}
    for report in selected:
        for forecast in report.get("forecasts", []):
            year = int(numeric(forecast.get("year"), 0))
            eps = numeric(forecast.get("eps"))
            if year and not math.isnan(eps):
                forecasts_by_year.setdefault(year, []).append(eps)
    dispersions = []
    for year, values in sorted(forecasts_by_year.items()):
        if len(values) < 2:
            continue
        ordered = sorted(values)
        median_value = ordered[len(ordered) // 2] if len(ordered) % 2 else sum(ordered[len(ordered)//2-1:len(ordered)//2+1]) / 2
        spread = None if median_value == 0 else (max(values) - min(values)) / abs(median_value)
        dispersions.append({"year": year, "count": len(values), "min": min(values), "max": max(values), "spread_ratio": spread})
    max_spread = max((item["spread_ratio"] for item in dispersions if item["spread_ratio"] is not None), default=None)
    dispersion_level = "预测样本不足" if max_spread is None else "分歧较小" if max_spread <= 0.1 else "存在一定分歧" if max_spread <= 0.25 else "分歧较大"
    return {
        "coverage_count": len(selected), "coverage_target": 3,
        "latest_date": latest_date, "age_days": age_days,
        "rating_counts": dict(rating_counts), "rating_consensus": rating_consensus,
        "focuses": _keyword_counts(selected, RESEARCH_FOCUS_RULES, "focus"),
        "risks": _keyword_counts(selected, RESEARCH_RISK_RULES, "risk"),
        "dispersions": dispersions, "dispersion_level": dispersion_level,
    }


def research_conclusion(comparison: dict[str, Any]) -> str:
    if not comparison:
        return "暂无足够研报形成对比。"
    coverage = comparison["coverage_count"]
    sentences = [
        f"当前取得最近{coverage}份研报，{comparison['rating_consensus']}，EPS预测{comparison['dispersion_level']}。"
    ]
    if comparison.get("focuses"):
        top = comparison["focuses"][0]
        sentences.append(f"机构最常讨论的是“{top['name']}”（{top['count']}/{coverage}份涉及）。")
    repeated_risks = [item for item in comparison.get("risks", []) if item["count"] >= 2]
    if repeated_risks:
        top = repeated_risks[0]
        sentences.append(f"重复出现最多的风险是“{top['name']}”（{top['count']}/{coverage}份涉及）。")
    else:
        sentences.append("三份研报中暂未识别出重复风险主题，仍需逐份阅读原始风险提示。")
    return "".join(sentences)


def report_forecast_text(report: dict[str, Any]) -> str:
    parts = []
    for row in report.get("forecasts", []):
        year = int(numeric(row.get("year"), 0))
        if year:
            parts.append(
                f"{year}E EPS {number_text(row.get('eps'), 2)} / "
                f"PE {number_text(row.get('pe'), 1, '×')}"
            )
    return " · ".join(parts)


def number_text(value: Any, digits: int = 1, suffix: str = "") -> str:
    number = numeric(value)
    return "—" if math.isnan(number) else f"{number:,.{digits}f}{suffix}"


def percent_text(value: Any, digits: int = 2) -> str:
    number = numeric(value)
    if math.isnan(number):
        return "—"
    return f"{number:+.{digits}f}%"


def large_number_text(value: Any) -> str:
    number = numeric(value)
    if math.isnan(number):
        return "—"
    if abs(number) >= 100_000_000:
        return f"{number / 100_000_000:,.1f} 亿"
    if abs(number) >= 10_000:
        return f"{number / 10_000:,.1f} 万"
    return f"{number:,.0f}"


def build_price_series(history: pd.DataFrame, limit: int = 260) -> list[dict[str, Any]]:
    frame = history.copy().sort_values("date")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["volume"] = pd.to_numeric(frame.get("volume"), errors="coerce")
    for window in (20, 60, 200):
        frame[f"ma{window}"] = frame["close"].rolling(window).mean()
    frame = frame.tail(limit)
    result = []
    for _, row in frame.iterrows():
        result.append(
            {
                "date": str(row["date"]),
                "close": None if pd.isna(row["close"]) else round(float(row["close"]), 4),
                "volume": None if pd.isna(row["volume"]) else float(row["volume"]),
                **{
                    f"ma{window}": (
                        None
                        if pd.isna(row[f"ma{window}"])
                        else round(float(row[f"ma{window}"]), 4)
                    )
                    for window in (20, 60, 200)
                },
            }
        )
    return result


def latest_business_mix(profile_payload: dict[str, Any], code: str) -> list[dict[str, Any]]:
    suffix_code = normalize_code(code, "suffix")
    rows = profile_payload.get("business_profiles", {}).get(suffix_code, [])
    products = [row for row in rows if str(row.get("MAINOP_TYPE")) == "2"]
    if not products:
        return []
    latest_date = max(str(row.get("REPORT_DATE") or "") for row in products)
    latest = [row for row in products if str(row.get("REPORT_DATE") or "") == latest_date]
    items = []
    other = 0.0
    for row in latest:
        name = str(row.get("ITEM_NAME") or "").strip()
        ratio = numeric(row.get("MBI_RATIO"), 0.0)
        if ratio > 1:
            ratio /= 100
        if ratio <= 0 or "抵消" in name or "抵销" in name:
            continue
        if ratio < 0.015 or "其他" in name or "补充" in name:
            other += max(0, ratio)
        else:
            items.append({"name": name, "ratio": ratio, "date": latest_date[:10]})
    if other >= 0.01:
        items.append({"name": "其他", "ratio": other, "date": latest_date[:10]})
    return sorted(items, key=lambda item: item["ratio"], reverse=True)[:6]


def latest_company_financial(
    payload: dict[str, Any], code: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    record = payload.get("records", {}).get(normalize_code(code, "suffix"), {})
    company = dict(record.get("company") or {})
    listing = dict(record.get("listing") or {})
    financials = list(record.get("financials") or [])
    latest = max(
        financials,
        key=lambda row: str(row.get("REPORT_DATE") or ""),
        default={},
    )
    return company, listing, dict(latest)


def compact_text(value: Any, limit: int = 360) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit].rstrip("，,。;； ") + "……"


def ratio_text(value: Any, digits: int = 1, signed: bool = False) -> str:
    number = numeric(value)
    if math.isnan(number):
        return "—"
    sign = "+" if signed and number > 0 else ""
    return f"{sign}{number:.{digits}f}%"


def build_fundamental_notes(financial: dict[str, Any]) -> list[dict[str, str]]:
    if not financial:
        return [{"level": "notice", "title": "暂无财报快照", "text": "请先更新公司与财务资料。"}]
    notes: list[dict[str, str]] = []
    revenue_yoy = numeric(financial.get("TOTALOPERATEREVETZ"))
    profit_yoy = numeric(financial.get("PARENTNETPROFITTZ"))
    cash_per_share = numeric(financial.get("MGJYXJJE"))
    debt_ratio = numeric(financial.get("ZCFZL"))
    if not math.isnan(revenue_yoy) and not math.isnan(profit_yoy):
        if revenue_yoy > 0 and profit_yoy > 0:
            notes.append({"level": "ok", "title": "收入与利润同步增长", "text": f"营收同比 {ratio_text(revenue_yoy, signed=True)}，归母净利润同比 {ratio_text(profit_yoy, signed=True)}。"})
        elif revenue_yoy > 0 >= profit_yoy:
            notes.append({"level": "warn", "title": "增收未增利", "text": f"营收同比 {ratio_text(revenue_yoy, signed=True)}，但归母净利润同比 {ratio_text(profit_yoy, signed=True)}。"})
        elif revenue_yoy <= 0 < profit_yoy:
            notes.append({"level": "notice", "title": "利润增长快于收入", "text": f"营收同比 {ratio_text(revenue_yoy, signed=True)}，归母净利润同比 {ratio_text(profit_yoy, signed=True)}，需核对利润改善来源。"})
        else:
            notes.append({"level": "warn", "title": "收入与利润承压", "text": f"营收同比 {ratio_text(revenue_yoy, signed=True)}，归母净利润同比 {ratio_text(profit_yoy, signed=True)}。"})
    if not math.isnan(cash_per_share):
        notes.append({"level": "ok" if cash_per_share >= 0 else "warn", "title": "经营现金流为正" if cash_per_share >= 0 else "经营现金流为负", "text": f"每股经营现金流 {cash_per_share:.2f} 元，建议结合应收账款和存货变化判断盈利含金量。"})
    if not math.isnan(debt_ratio):
        level = "warn" if debt_ratio >= 70 else "ok" if debt_ratio <= 45 else "notice"
        notes.append({"level": level, "title": "资产负债水平", "text": f"最新资产负债率 {ratio_text(debt_ratio)}，仍需结合有息负债及偿债期限观察。"})
    return notes[:3]


def percentile_rank(values: pd.Series, value: Any) -> tuple[float | None, int]:
    target = numeric(value)
    valid = pd.to_numeric(values, errors="coerce").dropna()
    valid = valid[(valid > 0) & (valid < 1000)]
    if math.isnan(target) or target <= 0 or valid.empty:
        return None, len(valid)
    return float((valid <= target).mean() * 100), len(valid)


def build_minimal_analysis(
    stock: pd.Series,
    classification: pd.Series,
    tags: pd.Series,
    business_mix: list[dict[str, Any]],
    financial: dict[str, Any],
    pool: pd.DataFrame,
) -> dict[str, Any]:
    tag_names = [scalar_text(tags.get(f"标签{i}")) for i in range(1, 4)]
    tag_names = [name for name in tag_names if name]
    top_business = business_mix[0] if business_mix else None
    business_evidence = "、".join(tag_names) or "暂无可靠产业标签"
    if top_business:
        business_evidence += f"；最大披露业务“{top_business['name']}”占 {top_business['ratio']:.1%}"

    revenue_yoy = numeric(financial.get("TOTALOPERATEREVETZ"))
    profit_yoy = numeric(financial.get("PARENTNETPROFITTZ"))
    cash_per_share = numeric(financial.get("MGJYXJJE"))
    roic = numeric(financial.get("ROIC"))
    if not math.isnan(revenue_yoy) and not math.isnan(profit_yoy):
        if revenue_yoy > 0 and profit_yoy > 0 and (math.isnan(cash_per_share) or cash_per_share >= 0):
            earnings_state, earnings_tone = "盈利改善", "good"
        elif revenue_yoy < 0 and profit_yoy < 0:
            earnings_state, earnings_tone = "盈利承压", "warn"
        else:
            earnings_state, earnings_tone = "盈利分化", "neutral"
        earnings_evidence = f"营收 {ratio_text(revenue_yoy, signed=True)}，归母净利 {ratio_text(profit_yoy, signed=True)}"
        if not math.isnan(cash_per_share):
            earnings_evidence += f"；每股经营现金流 {cash_per_share:.2f} 元"
        if not math.isnan(roic):
            earnings_evidence += f"；ROIC {ratio_text(roic)}"
    else:
        earnings_state, earnings_tone = "数据不足", "neutral"
        earnings_evidence = "缺少可比较的营收和利润同比数据"

    industry = str(stock.get("所属行业") or "").strip()
    peers = pool.loc[pool["所属行业"].astype(str).str.strip() == industry]
    peer_scope = f"股票池内{industry or '同行'}"
    pe_rank, peer_count = percentile_rank(peers.get("市盈率TTM", pd.Series(dtype=float)), stock.get("市盈率TTM"))
    if peer_count < 5:
        pe_rank, peer_count = percentile_rank(pool.get("市盈率TTM", pd.Series(dtype=float)), stock.get("市盈率TTM"))
        peer_scope = "当前股票池"
    pe = numeric(stock.get("市盈率TTM"))
    pb = numeric(stock.get("市净率"))
    if pe_rank is None:
        valuation_state, valuation_tone = "暂不可比", "neutral"
        valuation_evidence = "PE为负或可比样本不足，不强行判断高低"
    else:
        valuation_state = "相对偏低" if pe_rank <= 30 else "相对偏高" if pe_rank >= 70 else "相对居中"
        valuation_tone = "good" if pe_rank <= 30 else "warn" if pe_rank >= 70 else "neutral"
        valuation_evidence = f"PE {number_text(pe, 1, '×')}、PB {number_text(pb, 2, '×')}；位于{peer_scope}约 {pe_rank:.0f}% 分位（{peer_count}个有效样本）"

    label = str(classification.get("分类") or "未分类")
    trend = numeric(classification.get("trend_score"))
    rs = numeric(classification.get("rs_score"))
    position = numeric(classification.get("position_score"))
    exhaustion = numeric(classification.get("exhaustion_score"))
    market_tone = "good" if label in {"上升", "震荡上行"} else "warn" if label in {"下降", "顶部", "震荡下行"} else "neutral"
    market_evidence = f"趋势 {trend:.0f}，相对强弱 {rs:.0f}，价格位置 {position:.0f}"
    if not math.isnan(exhaustion) and exhaustion >= 75:
        market_evidence += f"；衰竭风险 {exhaustion:.0f}"

    vetoes = []
    if not math.isnan(revenue_yoy) and not math.isnan(profit_yoy) and revenue_yoy < 0 and profit_yoy < 0:
        vetoes.append("收入与利润同时下降")
    debt = numeric(financial.get("ZCFZL"))
    if not math.isnan(cash_per_share) and cash_per_share < 0 and not math.isnan(debt) and debt >= 70:
        vetoes.append("现金流为负且负债率偏高")
    if not math.isnan(exhaustion) and exhaustion >= 85:
        vetoes.append("价格衰竭风险很高")
    risk_gate = "；".join(vetoes) if vetoes else "未触发核心否决项"
    risk_tone = "warn" if vetoes else "good"

    coverage = sum([bool(tag_names), bool(financial), pe_rank is not None, not math.isnan(trend)])
    confidence = "高" if coverage == 4 and peer_count >= 8 else "中" if coverage >= 3 else "低"
    conclusion = f"业务聚焦{business_evidence.split('；')[0]}，{earnings_state}，估值{valuation_state}，市场状态为{label}。"
    return {
        "pillars": [
            {"name": "业务", "state": " / ".join(tag_names[:2]) or "待确认", "evidence": business_evidence, "tone": "good" if tag_names else "neutral"},
            {"name": "盈利", "state": earnings_state, "evidence": earnings_evidence, "tone": earnings_tone},
            {"name": "估值", "state": valuation_state, "evidence": valuation_evidence, "tone": valuation_tone},
            {"name": "市场", "state": label, "evidence": market_evidence, "tone": market_tone},
        ],
        "conclusion": conclusion,
        "risk_gate": risk_gate,
        "risk_tone": risk_tone,
        "confidence": confidence,
        "peer_count": peer_count,
    }


def build_risk_notes(classification: pd.Series) -> list[dict[str, str]]:
    notes: list[dict[str, str]] = []
    exhaustion = numeric(classification.get("exhaustion_score"))
    atr_ratio = numeric(classification.get("ATR_ratio"))
    ma20_dev = numeric(classification.get("price_ma20_dev"))
    ma200_dev = numeric(classification.get("price_ma200_dev"))
    pe = numeric(classification.get("市盈率TTM"))

    if not math.isnan(exhaustion) and exhaustion >= 75:
        notes.append({"level": "warn", "title": "衰竭风险偏高", "text": f"衰竭评分 {exhaustion:.1f}，强趋势中需留意高位波动。"})
    if not math.isnan(atr_ratio) and atr_ratio >= 0.08:
        notes.append({"level": "warn", "title": "波动率较高", "text": f"ATR约占价格 {atr_ratio:.1%}，短期价格波动显著。"})
    if not math.isnan(ma20_dev) and ma20_dev <= -0.08:
        notes.append({"level": "notice", "title": "短线回撤", "text": f"当前价格较MA20偏离 {ma20_dev:.1%}，短线动能正在修复。"})
    if not math.isnan(ma200_dev) and ma200_dev >= 0.50:
        notes.append({"level": "warn", "title": "长期乖离较大", "text": f"价格高于MA200约 {ma200_dev:.1%}，位置风险需要关注。"})
    if not math.isnan(pe) and pe >= 80:
        notes.append({"level": "warn", "title": "估值较高", "text": f"市盈率TTM约 {pe:.1f} 倍，对盈利兑现较敏感。"})
    if not notes:
        notes.append({"level": "ok", "title": "未发现突出风险信号", "text": "仍需结合基本面变化和市场环境持续跟踪。"})
    return notes[:5]


def classification_summary(row: pd.Series) -> str:
    label = str(row.get("分类") or "未分类")
    trend = numeric(row.get("trend_score"))
    direction = numeric(row.get("direction_score"))
    rs = numeric(row.get("rs_score"))
    parts = [f"当前处于“{label}”状态"]
    if not math.isnan(trend):
        parts.append(f"趋势强度 {trend:.0f}")
    if not math.isnan(direction):
        parts.append(f"方向动能 {direction:.0f}")
    if not math.isnan(rs):
        parts.append(f"相对强弱 {rs:.0f}")
    return "，".join(parts) + "。"


def esc(value: Any) -> str:
    return html.escape(scalar_text(value))


def safe_company_filename(name: Any, code: str = "") -> str:
    """生成Windows可用的公司名HTML文件名。"""
    cleaned = INVALID_FILENAME_RE.sub("_", str(name or "").strip()).rstrip(" .")
    if not cleaned:
        cleaned = normalize_code(code, "suffix") or "未命名股票"
    if cleaned.upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"{cleaned}_股票"
    return f"{cleaned[:100]}.html"


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    os.replace(temp_path, path)


def is_non_stock(row: pd.Series, tag_row: pd.Series) -> bool:
    """识别股票池中混入的指数，避免依赖可能过期或误判的标签状态。"""
    code = normalize_code(row.get("代码"), "suffix")
    name = str(row.get("名称") or "").strip()
    return (
        str(tag_row.get("标签状态") or "") == "非个股"
        or code in KNOWN_INDEX_CODES
        or name.endswith("指数")
        or name in {"上证50", "沪深300", "中证500", "科创50", "深证成指", "创业板指"}
    )


def render_page(context: dict[str, Any]) -> str:
    stock = context["stock"]
    classification = context["classification"]
    tags = context["tags"]
    price_series = context["price_series"]
    business_mix = context["business_mix"]
    risks = context["risks"]
    company = context.get("company") or {}
    listing = context.get("listing") or {}
    financial = context.get("financial") or {}
    fundamental_notes = context.get("fundamental_notes") or build_fundamental_notes(financial)
    minimal = context.get("minimal_analysis") or {
        "pillars": [],
        "conclusion": context.get("summary", ""),
        "risk_gate": "数据不足，暂不判断",
        "risk_tone": "neutral",
        "confidence": "低",
    }
    research_reports = list(context.get("research_reports") or [])[:3]
    research_comparison = build_research_comparison(
        research_reports,
        scalar_text(classification.get("截止交易日"))[:10] or context.get("generated_date"),
    )

    change = numeric(stock.get("涨跌幅"), 0.0)
    change_class = "negative" if change < 0 else "positive"
    label = esc(classification.get("分类", "未分类"))
    tag_cards = "".join(
        f"""
        <article class="tag-card">
          <div class="tag-score">{esc(tags.get(f'标签{i}相关度'))}</div>
          <h3>{esc(tags.get(f'标签{i}'))}</h3>
          <p>{esc(tags.get(f'标签{i}依据'))}</p>
        </article>
        """
        for i in range(1, 4)
        if scalar_text(tags.get(f"标签{i}"))
    )
    metric_items = [
        ("总市值", large_number_text(stock.get("市值"))),
        ("市盈率 TTM", number_text(stock.get("市盈率TTM"), 1, "×")),
        ("市净率", number_text(stock.get("市净率"), 2, "×")),
        ("换手率", str(stock.get("换手率") or "—")),
        ("20日表现", str(stock.get("20日涨跌幅") or "—")),
        ("60日表现", str(stock.get("60日涨跌幅") or "—")),
    ]
    metrics_html = "".join(
        f'<div class="metric"><span>{esc(name)}</span><strong>{esc(value)}</strong></div>'
        for name, value in metric_items
    )
    score_definitions = [
        ("趋势强度", "trend_score"),
        ("方向动能", "direction_score"),
        ("稳定性", "trend_stability_score"),
        ("ADX趋势", "adx_score"),
        ("相对强弱", "rs_score"),
        ("价格位置", "position_score"),
    ]
    score_rows = "".join(
        f"""
        <div class="score-row">
          <span>{esc(name)}</span>
          <div class="score-track"><i style="width:{max(0, min(100, numeric(classification.get(key), 0))):.1f}%"></i></div>
          <b>{numeric(classification.get(key), 0):.0f}</b>
        </div>
        """
        for name, key in score_definitions
    )
    risk_html = "".join(
        f'<article class="risk {esc(item["level"])}"><span></span><div><h3>{esc(item["title"])}</h3><p>{esc(item["text"])}</p></div></article>'
        for item in risks
    )
    business_rows = "".join(
        f'<li><span>{esc(item["name"])}</span><strong>{item["ratio"]:.1%}</strong></li>'
        for item in business_mix
    ) or '<li><span>暂无主营构成</span><strong>—</strong></li>'

    company_facts = [
        ("公司全称", company.get("ORG_NAME") or stock.get("名称")),
        ("注册地", company.get("REG_ADDRESS") or company.get("PROVINCE") or "—"),
        ("办公地址", company.get("ADDRESS") or "—"),
        ("法人代表", company.get("LEGAL_PERSON") or "—"),
        ("董事长 / 总经理", " / ".join(filter(None, [str(company.get("CHAIRMAN") or ""), str(company.get("PRESIDENT") or "")])) or "—"),
        ("成立 / 上市", " / ".join(filter(None, [str(listing.get("FOUND_DATE") or "")[:10], str(listing.get("LISTING_DATE") or "")[:10]])) or "—"),
        ("注册资本", number_text(company.get("REG_CAPITAL"), 2, " 万元")),
        ("员工人数", number_text(company.get("EMP_NUM"), 0, " 人")),
        ("所属行业", company.get("EM2016") or company.get("INDUSTRYCSRC1") or stock.get("所属行业") or "—"),
        ("公司网站", company.get("ORG_WEB") or "—"),
    ]
    company_facts_html = "".join(
        f'<div class="fact"><span>{esc(name)}</span><strong>{esc(value)}</strong></div>'
        for name, value in company_facts
    )
    financial_items = [
        ("营业收入", large_number_text(financial.get("TOTALOPERATEREVE"))),
        ("归母净利润", large_number_text(financial.get("PARENTNETPROFIT"))),
        ("扣非净利润", large_number_text(financial.get("KCFJCXSYJLR"))),
        ("基本每股收益", number_text(financial.get("EPSJB"), 2, " 元")),
        ("营收同比", ratio_text(financial.get("TOTALOPERATEREVETZ"), signed=True)),
        ("归母净利同比", ratio_text(financial.get("PARENTNETPROFITTZ"), signed=True)),
        ("加权ROE", ratio_text(financial.get("ROEJQ"))),
        ("销售毛利率", ratio_text(financial.get("XSMLL"))),
        ("销售净利率", ratio_text(financial.get("XSJLL"))),
        ("资产负债率", ratio_text(financial.get("ZCFZL"))),
        ("每股经营现金流", number_text(financial.get("MGJYXJJE"), 2, " 元")),
        ("流动比率", number_text(financial.get("LD"), 2)),
    ]
    financial_html = "".join(
        f'<div class="financial-metric"><span>{esc(name)}</span><strong>{esc(value)}</strong></div>'
        for name, value in financial_items
    )
    fundamental_html = "".join(
        f'<article class="insight {esc(item["level"])}"><i></i><div><h3>{esc(item["title"])}</h3><p>{esc(item["text"])}</p></div></article>'
        for item in fundamental_notes
    )
    report_name = financial.get("REPORT_DATE_NAME") or financial.get("REPORT_TYPE") or "暂无财报"
    report_date = str(financial.get("REPORT_DATE") or "")[:10] or "—"
    profile_text = compact_text(company.get("ORG_PROFILE") or company.get("BUSINESS_SCOPE") or "暂无公司简介，请先更新公司与财务资料。")
    pillar_html = "".join(
        f'<article class="pillar {esc(item["tone"])}"><div class="pillar-top"><span>{esc(item["name"])}</span><b>{esc(item["state"])}</b></div><p>{esc(item["evidence"])}</p></article>'
        for item in minimal.get("pillars", [])
    )
    research_html = "".join(
        f'''<article class="research-card"><div class="research-meta"><span>{esc(item.get("publish_date") or "—")}</span><span>{esc(item.get("organization") or "机构未标注")}</span><span>{esc(item.get("rating_change_name") or "变化未标注")}</span><b>{esc(item.get("rating") or "未评级")}</b></div>
        <h3>{esc(item.get("title") or "研报标题缺失")}</h3><ul>{"".join(f"<li>{esc(point)}</li>" for point in report_viewpoints(item))}</ul>
        {f'<p class="research-forecast">{esc(report_forecast_text(item))}</p>' if report_forecast_text(item) else ""}
        {f'<p class="research-risk">{esc(item.get("risk"))}</p>' if scalar_text(item.get("risk")).strip("：: ") not in {"", "风险提示", "风险因素"} else ""}</article>'''
        for item in research_reports
    ) or '<article class="research-empty">暂无公开个股研报，或尚未更新研报快照。</article>'
    if research_comparison:
        rating_text = " · ".join(f"{name} {count}份" for name, count in research_comparison["rating_counts"].items())
        focus_html = "".join(f'<span>{esc(item["name"])} <b>{item["count"]}/{research_comparison["coverage_count"]}</b></span>' for item in research_comparison["focuses"]) or "<em>暂无明确高频方向</em>"
        repeated_risks = [item for item in research_comparison["risks"] if item["count"] >= 2]
        risk_topic_html = "".join(f'<span>{esc(item["name"])} <b>{item["count"]}/{research_comparison["coverage_count"]}</b></span>' for item in repeated_risks) or "<em>暂无重复风险主题</em>"
        years = sorted({int(numeric(row.get("year"), 0)) for item in research_reports for row in item.get("forecasts", []) if int(numeric(row.get("year"), 0))})
        forecast_rows = "".join(
            f'<tr><th>{esc(item.get("organization") or "机构未标注")}</th>' + "".join(
                f'<td>{number_text(next((row.get("eps") for row in item.get("forecasts", []) if int(numeric(row.get("year"), 0)) == year), None), 2)}<small>{number_text(next((row.get("pe") for row in item.get("forecasts", []) if int(numeric(row.get("year"), 0)) == year), None), 1, "×")}</small></td>'
                for year in years
            ) + "</tr>" for item in research_reports
        )
        forecast_table = f'<div class="forecast-wrap"><table><thead><tr><th>机构</th>{"".join(f"<th>{year}E<br><small>EPS / PE</small></th>" for year in years)}</tr></thead><tbody>{forecast_rows}</tbody></table></div>' if years else '<p class="forecast-empty">当前快照暂无可比EPS/PE预测。</p>'
        age_text = "—" if research_comparison["age_days"] is None else f'{research_comparison["age_days"]}天前'
        research_summary_html = f'''<article class="research-comparison"><div class="eyebrow">STRUCTURED COMPARISON · 免费规则计算</div><h3>最新三份研报对比</h3>
        <div class="comparison-metrics"><div><span>研报覆盖</span><b>{research_comparison["coverage_count"]}/3</b></div><div><span>最新研报</span><b>{esc(research_comparison["latest_date"] or "—")}</b><small>{esc(age_text)}</small></div><div><span>评级一致性</span><b>{esc(research_comparison["rating_consensus"])}</b><small>{esc(rating_text)}</small></div><div><span>EPS预测分歧</span><b>{esc(research_comparison["dispersion_level"])}</b></div></div>
        <p class="comparison-conclusion">{esc(research_conclusion(research_comparison))}</p>
        <div class="topic-grid"><section><h4>高频关注方向</h4><div class="topic-chips">{focus_html}</div></section><section><h4>重复风险主题</h4><div class="topic-chips risk-topics">{risk_topic_html}</div></section></div>
        <h4 class="forecast-title">盈利预测对照</h4>{forecast_table}</article>'''
    else:
        research_summary_html = ""

    chart_payload = json.dumps(
        {
            "prices": price_series,
            "scores": [
                {"name": name, "value": max(0, min(100, numeric(classification.get(key), 0)))}
                for name, key in score_definitions
            ],
            "business": business_mix,
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="description" content="{esc(stock.get('名称'))}股票研究图示页面">
  <title>{esc(stock.get('名称'))} · 单股研究卡</title>
  <style>
    :root{{--bg:#07111d;--panel:#0d1b2a;--panel2:#102337;--line:#213a50;--text:#eaf4f5;--muted:#8ea9b7;--cyan:#52e0c4;--blue:#5da9ff;--amber:#ffc861;--red:#ff7d7d;--purple:#af8cff;}}
    *{{box-sizing:border-box}} html{{scroll-behavior:smooth}} body{{margin:0;background:radial-gradient(circle at 80% -10%,#173a4d 0,transparent 36%),linear-gradient(145deg,#06101b,#081522 48%,#06101a);color:var(--text);font-family:Inter,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;min-height:100vh}}
    body:before{{content:"";position:fixed;inset:0;pointer-events:none;opacity:.22;background-image:linear-gradient(rgba(255,255,255,.025) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.025) 1px,transparent 1px);background-size:42px 42px}}
    .page{{position:relative;width:min(1420px,calc(100% - 40px));margin:0 auto;padding:34px 0 54px}}
    .eyebrow{{color:var(--cyan);font-size:12px;letter-spacing:.18em;text-transform:uppercase;font-weight:800}}
    .hero{{display:grid;grid-template-columns:1.45fr .8fr;gap:22px;margin-top:18px}}
    .hero-main,.hero-side,.card{{background:linear-gradient(155deg,rgba(17,38,56,.95),rgba(9,25,39,.94));border:1px solid rgba(112,174,198,.18);box-shadow:0 22px 60px rgba(0,0,0,.24);border-radius:22px}}
    .hero-main{{padding:34px 36px;position:relative;overflow:hidden}} .hero-main:after{{content:"";position:absolute;width:240px;height:240px;border-radius:50%;background:var(--cyan);filter:blur(100px);opacity:.1;right:-90px;top:-90px}}
    .identity{{display:flex;align-items:flex-start;justify-content:space-between;gap:20px}} h1{{font-size:clamp(38px,5vw,68px);line-height:1;margin:8px 0 12px;letter-spacing:-.05em}} .code{{color:var(--muted);font-size:14px;letter-spacing:.08em}}
    .classification{{position:relative;z-index:1;padding:12px 16px;border-radius:14px;background:rgba(82,224,196,.1);border:1px solid rgba(82,224,196,.25);color:var(--cyan);font-weight:800;white-space:nowrap}}
    .price-line{{display:flex;align-items:flex-end;gap:18px;margin:34px 0 20px}} .price{{font-size:44px;font-weight:850;letter-spacing:-.04em}} .change{{font-size:19px;font-weight:750;padding-bottom:7px}} .negative{{color:var(--red)}} .positive{{color:var(--cyan)}}
    .summary{{max-width:760px;color:#c8d8de;font-size:17px;line-height:1.8;margin:0}}
    .hero-side{{padding:26px;display:flex;flex-direction:column;justify-content:space-between}} .side-title{{display:flex;justify-content:space-between;color:var(--muted);font-size:13px}} .market-cap{{font-size:36px;font-weight:800;margin:18px 0 3px}} .asof{{color:var(--muted);font-size:13px}}
    .quick-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:24px}} .quick{{padding:13px;background:rgba(255,255,255,.035);border-radius:12px}} .quick span{{display:block;color:var(--muted);font-size:11px;margin-bottom:5px}} .quick b{{font-size:16px}}
    .grid{{display:grid;grid-template-columns:1.35fr .65fr;gap:22px;margin-top:22px}} .card{{padding:25px}} .card-title{{display:flex;align-items:end;justify-content:space-between;gap:20px;margin-bottom:18px}} .card-title h2{{font-size:20px;margin:0}} .card-title p{{color:var(--muted);font-size:12px;margin:0}}
    .chart-box{{height:390px;position:relative}} canvas{{width:100%;height:100%;display:block}}
    .legend{{display:flex;flex-wrap:wrap;gap:14px;color:var(--muted);font-size:11px}} .legend i{{display:inline-block;width:15px;height:3px;border-radius:4px;margin-right:6px;vertical-align:middle}}
    .score-list{{display:grid;gap:15px;margin-top:10px}} .score-row{{display:grid;grid-template-columns:72px 1fr 30px;gap:11px;align-items:center;font-size:12px;color:var(--muted)}} .score-track{{height:7px;background:#172d40;border-radius:99px;overflow:hidden}} .score-track i{{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--blue),var(--cyan));box-shadow:0 0 15px rgba(82,224,196,.35)}} .score-row b{{color:var(--text);text-align:right}}
    .section-heading{{display:flex;justify-content:space-between;align-items:end;margin:38px 2px 17px}} .section-heading h2{{font-size:26px;margin:0}} .section-heading p{{margin:0;color:var(--muted);font-size:12px}}
    .decision-card{{margin-top:22px;padding:28px;border-radius:22px;background:linear-gradient(145deg,rgba(13,31,47,.98),rgba(8,24,37,.96));border:1px solid rgba(82,224,196,.2);box-shadow:0 22px 60px rgba(0,0,0,.2)}} .decision-head{{display:flex;justify-content:space-between;align-items:end;gap:20px}} .decision-head h2{{font-size:28px;margin:7px 0 0}} .confidence{{font-size:11px;color:var(--muted);padding:9px 12px;border:1px solid rgba(255,255,255,.09);border-radius:10px}} .confidence b{{color:var(--cyan);font-size:13px;margin-left:5px}} .decision-copy{{font-size:18px;line-height:1.75;color:#d5e4e8;margin:22px 0 17px}}
    .pillar-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:11px}} .pillar{{padding:17px;min-height:130px;border-radius:14px;background:rgba(255,255,255,.028);border-top:2px solid var(--blue)}} .pillar.good{{border-top-color:var(--cyan)}} .pillar.warn{{border-top-color:var(--amber)}} .pillar-top{{display:flex;justify-content:space-between;align-items:center;gap:9px}} .pillar-top span{{font-size:10px;color:var(--muted);letter-spacing:.14em}} .pillar-top b{{font-size:13px;text-align:right}} .pillar p{{font-size:11px;line-height:1.65;color:var(--muted);margin:13px 0 0}} .risk-gate{{display:flex;align-items:center;gap:12px;margin-top:12px;padding:13px 16px;border-radius:12px;background:rgba(93,169,255,.06);font-size:12px}} .risk-gate span{{font-size:9px;letter-spacing:.16em;color:var(--muted)}} .risk-gate.good strong{{color:var(--cyan)}} .risk-gate.warn{{background:rgba(255,200,97,.08)}} .risk-gate.warn strong{{color:var(--amber)}}
    .evidence-group{{margin-top:22px}} .evidence-group>summary{{list-style:none;cursor:pointer;display:flex;align-items:center;justify-content:space-between;padding:18px 21px;border-radius:16px;background:rgba(255,255,255,.035);border:1px solid rgba(255,255,255,.08);transition:.2s}} .evidence-group>summary::-webkit-details-marker{{display:none}} .evidence-group>summary:hover{{border-color:rgba(82,224,196,.35)}} .evidence-group>summary span{{display:flex;flex-direction:column;gap:4px}} .evidence-group>summary b{{font-size:14px}} .evidence-group>summary small{{font-size:10px;color:var(--muted)}} .evidence-group>summary i{{font-style:normal;font-size:23px;color:var(--cyan);transition:.2s}} .evidence-group[open]>summary i{{transform:rotate(45deg)}} .evidence-body{{padding-top:1px}}
    .company-grid{{display:grid;grid-template-columns:.9fr 1.1fr;gap:22px}} .company-card,.financial-card{{margin:0}} .profile-copy{{color:#c4d5dc;font-size:13px;line-height:1.85;margin:0 0 22px;padding:17px 18px;background:rgba(255,255,255,.025);border-left:3px solid var(--cyan);border-radius:0 12px 12px 0}}
    .fact-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.06);border-radius:14px;overflow:hidden}} .fact{{background:#0c1d2c;padding:14px 15px;min-width:0}} .fact span,.financial-metric span{{display:block;color:var(--muted);font-size:10px;margin-bottom:6px}} .fact strong{{font-size:12px;line-height:1.55;font-weight:650;overflow-wrap:anywhere}}
    .report-badge{{color:var(--cyan);border:1px solid rgba(82,224,196,.25);background:rgba(82,224,196,.08);padding:7px 9px;border-radius:8px;font-size:9px;letter-spacing:.16em;font-weight:800}} .financial-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}} .financial-metric{{padding:14px 12px;border-radius:12px;background:rgba(255,255,255,.035);min-width:0}} .financial-metric strong{{font-size:16px;overflow-wrap:anywhere}} .fundamental-list{{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin-top:14px}} .insight{{display:flex;gap:9px;padding:12px;background:rgba(255,255,255,.025);border-radius:11px}} .insight i{{flex:0 0 auto;width:7px;height:7px;border-radius:50%;margin-top:5px;background:var(--blue)}} .insight.ok i{{background:var(--cyan)}} .insight.warn i{{background:var(--amber)}} .insight h3{{font-size:11px;margin:0 0 5px}} .insight p{{font-size:10px;line-height:1.55;color:var(--muted);margin:0}}
    .tag-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}} .tag-card{{position:relative;min-height:170px;padding:23px;border:1px solid rgba(82,224,196,.18);background:linear-gradient(145deg,rgba(15,39,53,.94),rgba(9,28,42,.9));border-radius:18px;overflow:hidden}} .tag-card h3{{font-size:23px;margin:7px 0 13px}} .tag-card p{{font-size:12px;line-height:1.7;color:var(--muted);margin:0;max-width:90%}} .tag-score{{position:absolute;right:16px;top:12px;font-size:54px;font-weight:900;color:rgba(82,224,196,.09)}}
    .business-layout{{display:grid;grid-template-columns:260px 1fr;gap:24px;align-items:center}} .donut{{height:230px}} .business-list{{list-style:none;padding:0;margin:0;display:grid;gap:10px}} .business-list li{{display:flex;justify-content:space-between;gap:18px;padding:11px 0;border-bottom:1px solid rgba(255,255,255,.07);font-size:13px}} .business-list span{{color:#c5d6dc}} .business-list strong{{color:var(--cyan)}}
    .metric-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}} .metric{{padding:17px;background:rgba(255,255,255,.035);border:1px solid rgba(255,255,255,.04);border-radius:13px}} .metric span{{display:block;color:var(--muted);font-size:11px;margin-bottom:8px}} .metric strong{{font-size:19px}}
    .risk-list{{display:grid;gap:10px}} .risk{{display:flex;gap:13px;padding:15px;background:rgba(255,255,255,.025);border-radius:13px}} .risk>span{{width:8px;height:8px;border-radius:50%;margin-top:6px;background:var(--amber);box-shadow:0 0 12px rgba(255,200,97,.45)}} .risk.notice>span{{background:var(--blue)}} .risk.ok>span{{background:var(--cyan)}} .risk h3{{font-size:13px;margin:0 0 5px}} .risk p{{font-size:11px;color:var(--muted);line-height:1.6;margin:0}}
    .research-comparison{{padding:25px 27px;margin-bottom:13px;border-radius:18px;background:linear-gradient(145deg,rgba(22,49,65,.98),rgba(9,28,42,.96));border:1px solid rgba(82,224,196,.22)}}.research-comparison>h3{{font-size:21px;margin:7px 0 17px}}.comparison-metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}}.comparison-metrics>div{{padding:13px;border-radius:11px;background:rgba(255,255,255,.035)}}.comparison-metrics span,.comparison-metrics small{{display:block;color:var(--muted);font-size:9px}}.comparison-metrics b{{display:block;font-size:14px;margin:7px 0 4px}}.comparison-conclusion{{font-size:13px;line-height:1.8;color:#cfdee2;margin:18px 0;padding:14px 16px;border-left:3px solid var(--cyan);background:rgba(82,224,196,.045)}}.topic-grid{{display:grid;grid-template-columns:1fr 1fr;gap:15px}}.topic-grid h4,.forecast-title{{font-size:11px;color:var(--cyan);margin:0 0 9px}}.topic-chips{{display:flex;gap:7px;flex-wrap:wrap}}.topic-chips span,.topic-chips em{{font-size:10px;font-style:normal;padding:7px 9px;border-radius:8px;background:rgba(93,169,255,.09);color:#c8dce3}}.topic-chips b{{color:var(--cyan)}}.risk-topics span{{background:rgba(255,200,97,.08)}}.risk-topics b{{color:var(--amber)}}.forecast-title{{margin-top:20px}}.forecast-wrap{{overflow-x:auto}}.forecast-wrap table{{width:100%;border-collapse:collapse;font-size:11px}}.forecast-wrap th,.forecast-wrap td{{padding:10px;border:1px solid rgba(255,255,255,.07);text-align:center}}.forecast-wrap th:first-child{{text-align:left}}.forecast-wrap td small,.forecast-wrap thead small{{display:block;color:var(--muted);font-size:8px;margin-top:3px}}.forecast-empty{{color:var(--muted);font-size:11px}}.research-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:13px}}.research-card,.research-empty{{padding:21px;border-radius:17px;background:linear-gradient(145deg,rgba(16,35,53,.97),rgba(9,26,40,.95));border:1px solid rgba(93,169,255,.17)}}.research-meta{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;color:var(--muted);font-size:10px}}.research-meta b{{margin-left:auto;color:var(--cyan);padding:4px 7px;border-radius:6px;background:rgba(82,224,196,.08)}}.research-card h3{{font-size:16px;line-height:1.55;margin:14px 0 12px}}.research-card ul{{margin:0;padding-left:18px;color:#bcd0d7}}.research-card li{{font-size:12px;line-height:1.65;padding:5px 0}}.research-forecast,.research-risk{{font-size:10px;line-height:1.6;margin:13px 0 0;padding-top:10px;border-top:1px solid rgba(255,255,255,.07)}}.research-forecast{{color:var(--cyan)}}.research-risk{{color:var(--amber)}}.research-empty{{grid-column:1/-1;color:var(--muted);font-size:13px}}
    footer{{display:flex;justify-content:space-between;gap:20px;margin-top:35px;padding:20px 4px;color:#668493;font-size:11px;border-top:1px solid rgba(255,255,255,.06)}}
    @media(max-width:1120px){{.company-grid{{grid-template-columns:1fr}}.pillar-grid{{grid-template-columns:1fr 1fr}}}}
    @media(max-width:980px){{.hero,.grid{{grid-template-columns:1fr}}.tag-grid{{grid-template-columns:1fr 1fr}}.business-layout{{grid-template-columns:220px 1fr}}.research-grid,.topic-grid{{grid-template-columns:1fr}}.comparison-metrics{{grid-template-columns:1fr 1fr}}}}
    @media(max-width:640px){{.page{{width:min(100% - 22px,1420px);padding-top:18px}}.hero-main{{padding:25px 21px}}.identity{{display:block}}.classification{{display:inline-block;margin-top:14px}}.hero-side,.card{{padding:19px}}.decision-card{{padding:20px}}.decision-head{{display:block}}.confidence{{display:inline-block;margin-top:12px}}.decision-copy{{font-size:15px}}.pillar-grid{{grid-template-columns:1fr}}.tag-grid{{grid-template-columns:1fr}}.business-layout{{grid-template-columns:1fr}}.metric-grid,.fact-grid,.financial-grid{{grid-template-columns:1fr 1fr}}.fundamental-list{{grid-template-columns:1fr}}.chart-box{{height:310px}}footer{{display:block;line-height:1.8}}}}
    @media print{{body{{background:#fff;color:#12202c}}body:before{{display:none}}.hero-main,.hero-side,.card,.tag-card{{box-shadow:none;background:#fff;border-color:#ccd9df}}.summary,.card-title p,.tag-card p,.score-row,.asof,.code,.business-list span,.risk p,footer{{color:#526875}}.page{{width:100%;padding:0}}}}
  </style>
</head>
<body>
<main class="page">
  <div class="eyebrow">Equity intelligence · 单股研究图示</div>
  <section class="hero">
    <div class="hero-main">
      <div class="identity"><div><h1>{esc(stock.get('名称'))}</h1><div class="code">{esc(stock.get('代码'))} · {esc(stock.get('市场'))} · {esc(stock.get('所属行业'))}</div></div><div class="classification">{label}</div></div>
      <div class="price-line"><div class="price">{esc(stock.get('最新价'))}</div><div class="change {change_class}">{esc(stock.get('涨跌幅'))}</div></div>
      <p class="summary">{esc(context['summary'])}</p>
    </div>
    <aside class="hero-side">
      <div><div class="side-title"><span>总市值</span><span>MARKET CAP</span></div><div class="market-cap">{large_number_text(stock.get('市值'))}</div><div class="asof">数据截止 {esc(classification.get('截止交易日') or context['generated_date'])}</div></div>
      <div class="quick-grid"><div class="quick"><span>20日表现</span><b>{esc(stock.get('20日涨跌幅'))}</b></div><div class="quick"><span>60日表现</span><b>{esc(stock.get('60日涨跌幅'))}</b></div><div class="quick"><span>换手率</span><b>{esc(stock.get('换手率'))}</b></div><div class="quick"><span>振幅</span><b>{esc(stock.get('振幅'))}</b></div></div>
    </aside>
  </section>

  <section class="decision-card">
    <div class="decision-head"><div><div class="eyebrow">MINIMUM SUFFICIENT VIEW</div><h2>四维核心判断</h2></div><div class="confidence">数据置信度 <b>{esc(minimal.get('confidence'))}</b></div></div>
    <p class="decision-copy">{esc(minimal.get('conclusion'))}</p>
    <div class="pillar-grid">{pillar_html}</div>
    <div class="risk-gate {esc(minimal.get('risk_tone'))}"><span>否决项</span><strong>{esc(minimal.get('risk_gate'))}</strong></div>
  </section>

  <div class="section-heading"><h2>最新研报观点</h2><p>按发布日期展示最近三份公开个股研报</p></div>
  {research_summary_html}
  <section class="research-grid">{research_html}</section>

  <details class="evidence-group">
    <summary><span><b>展开完整分析证据</b><small>公司资料、财报指标、价格图表、产业依据与风险明细</small></span><i>＋</i></summary>
    <div class="evidence-body">

  <div class="section-heading"><h2>公司与最新财报</h2><p>公司登记信息与最近一期主要财务指标</p></div>
  <section class="company-grid">
    <article class="card company-card"><div class="card-title"><div><h2>公司概况</h2><p>{esc(company.get('SECURITY_TYPE') or stock.get('市场') or '')}</p></div></div><p class="profile-copy">{esc(profile_text)}</p><div class="fact-grid">{company_facts_html}</div></article>
    <article class="card financial-card"><div class="card-title"><div><h2>{esc(report_name)}</h2><p>报告期 {esc(report_date)} · 公告 {esc(str(financial.get('NOTICE_DATE') or '')[:10] or '—')}</p></div><div class="report-badge">LATEST</div></div><div class="financial-grid">{financial_html}</div><div class="fundamental-list">{fundamental_html}</div></article>
  </section>

  <section class="grid">
    <article class="card"><div class="card-title"><div><h2>价格趋势</h2><p>最近 {len(price_series)} 个交易日 · 前复权</p></div><div class="legend"><span><i style="background:#52e0c4"></i>收盘</span><span><i style="background:#5da9ff"></i>MA20</span><span><i style="background:#ffc861"></i>MA60</span><span><i style="background:#af8cff"></i>MA200</span></div></div><div class="chart-box"><canvas id="priceChart" aria-label="价格与均线趋势图"></canvas></div></article>
    <article class="card"><div class="card-title"><div><h2>趋势评分</h2><p>0—100 标准化评分</p></div></div><div class="chart-box" style="height:205px"><canvas id="radarChart" aria-label="趋势评分雷达图"></canvas></div><div class="score-list">{score_rows}</div></article>
  </section>

  <div class="section-heading"><h2>产业定位</h2><p>相关度来自主营收入、细分行业与业务题材</p></div>
  <section class="tag-grid">{tag_cards}</section>

  <section class="grid">
    <article class="card"><div class="card-title"><div><h2>主营收入构成</h2><p>{esc(business_mix[0]['date'] if business_mix else '暂无报告日期')}</p></div></div><div class="business-layout"><div class="donut"><canvas id="businessChart" aria-label="主营收入构成环形图"></canvas></div><ul class="business-list">{business_rows}</ul></div></article>
    <article class="card"><div class="card-title"><div><h2>关键指标</h2><p>行情与估值快照</p></div></div><div class="metric-grid">{metrics_html}</div></article>
  </section>

  <div class="section-heading"><h2>风险观察</h2><p>自动提示，不构成投资建议</p></div>
  <section class="card"><div class="risk-list">{risk_html}</div></section>
    </div>
  </details>
  <footer><span>数据来源：本地行情历史、综合评级、产业标签、公开F10资料及东方财富Choice研报展示数据</span><span>生成时间：{esc(context['generated_at'])} · 研报观点归属于原发布机构 · 仅供研究参考</span></footer>
</main>
<script id="chartData" type="application/json">{chart_payload}</script>
<script>
const DATA=JSON.parse(document.getElementById('chartData').textContent);
const COLORS={{close:'#52e0c4',ma20:'#5da9ff',ma60:'#ffc861',ma200:'#af8cff',grid:'rgba(142,169,183,.14)',text:'#7895a4'}};
function setup(canvas){{const dpr=Math.min(devicePixelRatio||1,2),r=canvas.getBoundingClientRect();canvas.width=r.width*dpr;canvas.height=r.height*dpr;const c=canvas.getContext('2d');c.scale(dpr,dpr);return{{c,w:r.width,h:r.height}}}}
function line(c,pts,color,width=2){{c.beginPath();let started=false;pts.forEach(p=>{{if(!p)return;if(!started){{c.moveTo(...p);started=true}}else c.lineTo(...p)}});c.strokeStyle=color;c.lineWidth=width;c.stroke()}}
function priceChart(){{const canvas=document.getElementById('priceChart'),{{c,w,h}}=setup(canvas),pad={{l:48,r:16,t:18,b:30}},rows=DATA.prices,vals=rows.flatMap(r=>[r.close,r.ma20,r.ma60,r.ma200]).filter(v=>v!==null),min=Math.min(...vals),max=Math.max(...vals),x=i=>pad.l+i*(w-pad.l-pad.r)/(rows.length-1),y=v=>pad.t+(max-v)*(h-pad.t-pad.b)/(max-min||1);c.clearRect(0,0,w,h);c.font='11px system-ui';for(let i=0;i<5;i++){{const yy=pad.t+i*(h-pad.t-pad.b)/4,value=max-i*(max-min)/4;c.strokeStyle=COLORS.grid;c.beginPath();c.moveTo(pad.l,yy);c.lineTo(w-pad.r,yy);c.stroke();c.fillStyle=COLORS.text;c.fillText(value.toFixed(0),4,yy+4)}}[['close',COLORS.close,2.5],['ma20',COLORS.ma20,1.5],['ma60',COLORS.ma60,1.5],['ma200',COLORS.ma200,1.5]].forEach(([k,color,width])=>line(c,rows.map((r,i)=>r[k]===null?null:[x(i),y(r[k])]),color,width));[0,Math.floor((rows.length-1)/2),rows.length-1].forEach(i=>{{c.fillStyle=COLORS.text;c.fillText(rows[i].date.slice(5),x(i)-18,h-7)}})}}
function radarChart(){{const canvas=document.getElementById('radarChart'),{{c,w,h}}=setup(canvas),items=DATA.scores,n=items.length,cx=w/2,cy=h/2+3,R=Math.min(w,h)*.34;for(let ring=1;ring<=4;ring++){{c.beginPath();items.forEach((_,i)=>{{const a=-Math.PI/2+i*Math.PI*2/n,r=R*ring/4,p=[cx+Math.cos(a)*r,cy+Math.sin(a)*r];i?c.lineTo(...p):c.moveTo(...p)}});c.closePath();c.strokeStyle=COLORS.grid;c.stroke()}}items.forEach((item,i)=>{{const a=-Math.PI/2+i*Math.PI*2/n;c.strokeStyle=COLORS.grid;c.beginPath();c.moveTo(cx,cy);c.lineTo(cx+Math.cos(a)*R,cy+Math.sin(a)*R);c.stroke();c.fillStyle=COLORS.text;c.font='10px system-ui';c.textAlign=Math.cos(a)>.2?'left':Math.cos(a)<-.2?'right':'center';c.fillText(item.name,cx+Math.cos(a)*(R+15),cy+Math.sin(a)*(R+15)+3)}});c.beginPath();items.forEach((item,i)=>{{const a=-Math.PI/2+i*Math.PI*2/n,r=R*item.value/100,p=[cx+Math.cos(a)*r,cy+Math.sin(a)*r];i?c.lineTo(...p):c.moveTo(...p)}});c.closePath();c.fillStyle='rgba(82,224,196,.17)';c.fill();c.strokeStyle=COLORS.close;c.lineWidth=2;c.stroke()}}
function businessChart(){{const canvas=document.getElementById('businessChart'),{{c,w,h}}=setup(canvas),items=DATA.business,colors=['#52e0c4','#5da9ff','#ffc861','#af8cff','#ff8f70','#597486'],cx=w/2,cy=h/2,R=Math.min(w,h)*.39,r=R*.57,total=items.reduce((s,i)=>s+i.ratio,0)||1;let a=-Math.PI/2;items.forEach((item,i)=>{{const next=a+item.ratio/total*Math.PI*2;c.beginPath();c.arc(cx,cy,R,a,next);c.arc(cx,cy,r,next,a,true);c.closePath();c.fillStyle=colors[i%colors.length];c.fill();a=next}});c.fillStyle='#eaf4f5';c.textAlign='center';c.font='700 24px system-ui';c.fillText((total*100).toFixed(0)+'%',cx,cy+4);c.fillStyle=COLORS.text;c.font='10px system-ui';c.fillText('已披露构成',cx,cy+22)}}
function draw(){{priceChart();radarChart();businessChart()}} let timer;addEventListener('resize',()=>{{clearTimeout(timer);timer=setTimeout(draw,120)}});draw();
</script>
</body></html>"""


def load_page_sources() -> dict[str, Any]:
    pool_file = resolve_input(None, config_key="stock_pool")
    pool = read_csv_auto(pool_file, dtype=str)
    classification_file = latest_matching_file(
        PROJECT_DIR, str(config_value("files", "classification_pattern"))
    )
    tag_file = latest_industry_tag_file()
    profile_file = latest_company_profile_file()
    profile_payload = json.loads(profile_file.read_text(encoding="utf-8"))
    financial_file = latest_company_financial_file()
    financial_payload = (
        json.loads(financial_file.read_text(encoding="utf-8"))
        if financial_file
        else {"records": {}}
    )
    research_file = latest_research_report_file()
    research_payload = (
        json.loads(research_file.read_text(encoding="utf-8"))
        if research_file
        else {"records": {}}
    )
    return {
        "pool": pool,
        "classification_frame": read_csv_auto(classification_file, dtype=str),
        "tag_frame": read_csv_auto(tag_file, dtype=str),
        "profile_payload": profile_payload,
        "financial_payload": financial_payload,
        "research_payload": research_payload,
        "history_dir": project_path(config_value("files", "history_dir", "data/history")),
        "source_paths": {
            "pool": str(pool_file),
            "classification": str(classification_file),
            "tags": str(tag_file),
            "profiles": str(profile_file),
            "company_financials": str(financial_file) if financial_file else "",
            "research_reports": str(research_file) if research_file else "",
        },
    }


def build_context(code: str, sources: dict[str, Any] | None = None) -> dict[str, Any]:
    target = normalize_code(code, "suffix")
    sources = sources or load_page_sources()
    stock = find_stock_row(sources["pool"], target, "股票池")
    classification = find_stock_row(
        sources["classification_frame"], target, "分类总表"
    )
    tags = find_stock_row(sources["tag_frame"], target, "产业标签")
    history = load_history(
        sources["history_dir"], target, verify_checksum=True
    )
    if history is None or history.empty:
        raise FileNotFoundError(f"正式历史库缺少行情：{target}")
    company, listing, financial = latest_company_financial(
        sources["financial_payload"], target
    )
    business_mix = latest_business_mix(sources["profile_payload"], target)
    research_reports = (
        sources.get("research_payload", {}).get("records", {}).get(target, {}).get("reports", [])
    )

    return {
        "stock": stock,
        "classification": classification,
        "tags": tags,
        "price_series": build_price_series(history),
        "business_mix": business_mix,
        "company": company,
        "listing": listing,
        "financial": financial,
        "research_reports": research_reports,
        "fundamental_notes": build_fundamental_notes(financial),
        "minimal_analysis": build_minimal_analysis(
            stock,
            classification,
            tags,
            business_mix,
            financial,
            sources["pool"],
        ),
        "risks": build_risk_notes(classification),
        "summary": classification_summary(classification),
        "generated_date": datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sources": sources["source_paths"],
    }


def render_index_page(entries: list[dict[str, str]], generated_at: str) -> str:
    cards = "".join(
        f"""<a class="stock" href="{esc(entry['filename'])}" data-search="{esc(entry['search'])}">
          <div><h2>{esc(entry['name'])}</h2><p>{esc(entry['code'])} · {esc(entry['market'])}</p></div>
          <div class="right"><b>{esc(entry['classification'])}</b><span>{esc(entry['tag'])}</span></div>
        </a>"""
        for entry in entries
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>沪深股票研究页面</title><style>
    :root{{--bg:#07111d;--panel:#0d1b2a;--text:#eaf4f5;--muted:#8ea9b7;--cyan:#52e0c4}}*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at 80% -20%,#194257,transparent 35%),var(--bg);color:var(--text);font-family:Inter,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}}main{{width:min(1050px,calc(100% - 28px));margin:auto;padding:42px 0 60px}}header{{display:flex;justify-content:space-between;align-items:end;gap:20px;margin-bottom:28px}}h1{{font-size:clamp(34px,6vw,64px);letter-spacing:-.05em;margin:4px 0}}header p,.count{{color:var(--muted);font-size:13px}}input{{width:100%;background:#102337;border:1px solid #24445b;color:var(--text);padding:16px 18px;border-radius:14px;font-size:16px;outline:none;margin-bottom:18px}}input:focus{{border-color:var(--cyan);box-shadow:0 0 0 3px rgba(82,224,196,.08)}}.list{{display:grid;grid-template-columns:repeat(2,1fr);gap:11px}}.stock{{display:flex;justify-content:space-between;align-items:center;gap:15px;padding:18px 20px;border-radius:15px;background:linear-gradient(145deg,#102337,#0a1927);border:1px solid rgba(112,174,198,.15);text-decoration:none;color:var(--text);transition:.2s}}.stock:hover{{transform:translateY(-2px);border-color:rgba(82,224,196,.5)}}.stock h2{{font-size:17px;margin:0 0 6px}}.stock p{{font-size:11px;color:var(--muted);margin:0}}.right{{text-align:right}}.right b{{display:block;color:var(--cyan);font-size:12px;margin-bottom:6px}}.right span{{font-size:11px;color:var(--muted)}}footer{{margin-top:30px;color:#638190;font-size:11px}}@media(max-width:650px){{header{{display:block}}.list{{grid-template-columns:1fr}}}}
    </style></head><body><main><header><div><div class="count">STOCK RESEARCH LIBRARY</div><h1>沪深股票研究页</h1><p>按名称、代码、分类或产业标签搜索</p></div><div class="count">共 {len(entries)} 只个股</div></header><input id="search" type="search" placeholder="搜索兆易创新、603986、上升、存储…" aria-label="搜索股票"><section class="list" id="list">{cards}</section><footer>生成时间：{esc(generated_at)} · 页面全部离线可用</footer></main><script>const q=document.getElementById('search'),cards=[...document.querySelectorAll('.stock')];q.addEventListener('input',()=>{{const v=q.value.trim().toLowerCase();cards.forEach(c=>c.hidden=v&&!c.dataset.search.toLowerCase().includes(v))}});</script></body></html>"""


def generate_all_pages(output_dir: Path) -> dict[str, Any]:
    sources = load_page_sources()
    previous_generated_files: set[str] = set()
    previous_manifest = output_dir / "generation_manifest.json"
    if previous_manifest.exists():
        try:
            previous_payload = json.loads(previous_manifest.read_text(encoding="utf-8"))
            previous_generated_files = {
                str(item["filename"])
                for item in previous_payload.get("successes", [])
                if item.get("filename")
            }
        except (OSError, ValueError, TypeError):
            previous_generated_files = set()
    successes: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    used_filenames: set[str] = set()
    rows = sources["pool"][["代码", "名称", "市场"]].drop_duplicates("代码")
    total = len(rows)
    for number, (_, row) in enumerate(rows.iterrows(), start=1):
        code = normalize_code(row["代码"], "suffix")
        name = str(row["名称"]).strip()
        try:
            tag_row = find_stock_row(sources["tag_frame"], code, "产业标签")
            if is_non_stock(row, tag_row):
                skipped.append({"code": code, "name": name, "reason": "非个股"})
                continue
            context = build_context(code, sources)
            filename = safe_company_filename(name, code)
            if filename.casefold() in used_filenames:
                filename = safe_company_filename(f"{name}_{code}", code)
            used_filenames.add(filename.casefold())
            atomic_write_text(output_dir / filename, render_page(context))
            successes.append(
                {
                    "code": code,
                    "name": name,
                    "market": str(row.get("市场") or ""),
                    "classification": str(context["classification"].get("分类") or ""),
                    "tag": str(context["tags"].get("标签1") or ""),
                    "filename": filename,
                    "search": " ".join(
                        str(value or "")
                        for value in (
                            name,
                            code,
                            context["classification"].get("分类"),
                            context["tags"].get("标签1"),
                            context["tags"].get("标签2"),
                            context["tags"].get("标签3"),
                        )
                    ),
                }
            )
        except Exception as exc:
            failed.append({"code": code, "name": name, "reason": str(exc)})
        if number % 25 == 0 or number == total:
            print(
                f"[页面生成] {number}/{total} | 成功 {len(successes)} | "
                f"跳过 {len(skipped)} | 失败 {len(failed)}",
                flush=True,
            )
    successes.sort(key=lambda item: item["code"])
    current_generated_files = {item["filename"] for item in successes}
    stale_generated_files = previous_generated_files - current_generated_files
    legacy_code_files = {
        path.name
        for path in output_dir.glob("*.html")
        if LEGACY_CODE_FILENAME_RE.fullmatch(path.name)
    }
    removed_pages: list[str] = []
    for filename in sorted(stale_generated_files | legacy_code_files):
        path = output_dir / filename
        if path.is_file():
            path.unlink()
            removed_pages.append(filename)
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    atomic_write_text(output_dir / "index.html", render_index_page(successes, generated_at))
    manifest = {
        "generated_at": generated_at,
        "source_rows": total,
        "successes": successes,
        "skipped": skipped,
        "failed": failed,
        "removed_pages": removed_pages,
    }
    atomic_write_text(
        output_dir / "generation_manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )
    return manifest


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="生成单只或全部股票的离线图示研究页面")
    parser.add_argument("code", nargs="?", help="股票代码，例如603986.SH")
    parser.add_argument("--all", action="store_true", help="批量生成股票池中的全部个股")
    parser.add_argument("--output-dir", default="data/output/stock_pages")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    output_dir = project_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.all:
        manifest = generate_all_pages(output_dir)
        print(f"目录页：{output_dir / 'index.html'}")
        print(f"成功：{len(manifest['successes'])}")
        print(f"跳过非个股：{len(manifest['skipped'])}")
        print(f"失败：{len(manifest['failed'])}")
        return 1 if manifest["failed"] else 0
    if not args.code:
        raise ValueError("请提供股票代码，或使用 --all 批量生成")
    code = normalize_code(args.code, "suffix")
    context = build_context(code)
    output_path = output_dir / safe_company_filename(context["stock"]["名称"], code)
    atomic_write_text(output_path, render_page(context))
    print(f"单股页面：{output_path}")
    print(f"股票：{context['stock']['名称']} {code}")
    print(f"分类：{context['classification']['分类']}")
    print(f"行情点数：{len(context['price_series'])}")
    print(f"主营项目：{len(context['business_mix'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
