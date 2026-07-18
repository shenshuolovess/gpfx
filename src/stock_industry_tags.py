"""为股票池生成最多三个有证据、可审计的细分产业标签。"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from pipeline_config import PROJECT_DIR, config_value, project_path, resolve_input
from stock_utils import dated_output_path, normalize_code, read_csv_auto, write_csv


EASTMONEY_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DEFAULT_TAG_CONFIG = PROJECT_DIR / "industry_tags.toml"
NON_STOCK_NAME_RE = re.compile(r"指数|上证综指|深证成指|创业板指|科创50|沪深300|中证\d+|ETF", re.I)
ROMAN_SUFFIX_RE = re.compile(r"(?:Ⅱ|Ⅲ|IV|II|III)$", re.I)


@dataclass
class TagEvidence:
    tag: str
    score: float
    evidence: list[str] = field(default_factory=list)
    base_score: float = field(init=False)

    def __post_init__(self) -> None:
        self.base_score = self.score

    def merge(self, score: float, evidence: str) -> None:
        if evidence not in self.evidence:
            self.evidence.append(evidence)
        # 多条独立证据最多额外加6分，但不能因为堆概念超过100。
        self.base_score = max(self.base_score, score)
        agreement_bonus = min(6, max(0, len(self.evidence) - 1) * 3)
        self.score = min(100, self.base_score + agreement_bonus)


def load_tag_config(path: str | Path = DEFAULT_TAG_CONFIG) -> dict[str, Any]:
    config_path = project_path(path)
    with config_path.open("rb") as handle:
        return tomllib.load(handle)


def normalize_tag(raw: str, aliases: dict[str, str]) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    text = aliases.get(text, text)
    text = re.sub(r"概念$", "", text).strip()
    text = ROMAN_SUFFIX_RE.sub("", text).strip()
    return aliases.get(text, text)


def is_excluded(raw: str, tag: str, filters: dict[str, Any]) -> bool:
    excluded_exact = set(filters.get("excluded_exact", []))
    if raw in excluded_exact or tag in excluded_exact:
        return True
    return any(part in raw or part in tag for part in filters.get("excluded_contains", []))


def is_specific(tag: str, filters: dict[str, Any]) -> bool:
    return any(keyword.lower() in tag.lower() for keyword in filters.get("specific_keywords", []))


def _add_candidate(
    candidates: dict[str, TagEvidence], tag: str, score: float, evidence: str
) -> None:
    if not tag:
        return
    if tag not in candidates:
        candidates[tag] = TagEvidence(tag=tag, score=min(100, score), evidence=[evidence])
    else:
        candidates[tag].merge(score, evidence)


def score_tags(
    industry: str,
    boards: list[dict[str, Any]],
    config: dict[str, Any],
    business_items: list[dict[str, Any]] | None = None,
) -> list[TagEvidence]:
    aliases = config.get("aliases", {})
    filters = config.get("filters", {})
    broad = set(filters.get("broad_labels", []))
    candidates: dict[str, TagEvidence] = {}

    latest_products: list[dict[str, Any]] = []
    product_rows = [
        item for item in (business_items or []) if str(item.get("MAINOP_TYPE")) == "2"
    ]
    if product_rows:
        latest_date = max(str(item.get("REPORT_DATE") or "") for item in product_rows)
        latest_products = [
            item for item in product_rows if str(item.get("REPORT_DATE") or "") == latest_date
        ]

    business_keywords = config.get("business_keywords", {})
    business_excluded_exact = set(filters.get("business_excluded_exact", []))
    business_excluded_contains = filters.get("business_excluded_contains", [])
    for item in latest_products:
        raw_name = str(item.get("ITEM_NAME") or "").strip()
        if not raw_name or any(
            word in raw_name for word in ("其他", "补充", "抵消", "未分配", "合计")
        ):
            continue
        try:
            ratio = float(item.get("MBI_RATIO"))
        except (TypeError, ValueError):
            ratio = 0.0
        if ratio > 1:
            ratio /= 100
        if ratio < 0.05:
            continue

        business_tag = ""
        for label, keywords in business_keywords.items():
            if any(str(keyword).lower() in raw_name.lower() for keyword in keywords):
                business_tag = str(label)
                break
        if not business_tag:
            cleaned = re.sub(r"[（(].*?[）)]", "", raw_name)
            cleaned = re.sub(r"产品销售收入|销售收入|营业收入|收入", "", cleaned)
            cleaned = re.sub(r"业务$|产品$", "", cleaned).strip(" ：:-")
            if cleaned and len(cleaned) <= 14:
                business_tag = normalize_tag(cleaned, aliases)
        if (
            not business_tag
            or business_tag in broad
            or business_tag in business_excluded_exact
            or any(part in business_tag for part in business_excluded_contains)
            or (len(business_tag) <= 2 and business_tag not in set(business_keywords))
        ):
            continue
        if ratio >= 0.50:
            score = 98
        elif ratio >= 0.30:
            score = 94
        elif ratio >= 0.15:
            score = 90
        elif ratio >= 0.05:
            score = 82
        else:
            score = 76
        _add_candidate(
            candidates,
            business_tag,
            score,
            f"最新主营产品：{raw_name}（{latest_date[:10]}，收入占比{ratio:.1%}）",
        )

    industry = str(industry or "").strip()
    industry_tag = normalize_tag(industry, aliases)
    if industry_tag and industry_tag != "-" and not is_excluded(industry, industry_tag, filters):
        industry_score = 76 if industry_tag not in broad else 58
        _add_candidate(candidates, industry_tag, industry_score, f"输入文件所属行业：{industry}")

    ranked = sorted(boards, key=lambda item: int(item.get("BOARD_RANK") or 9999))
    # 前三项通常是一级、二级、三级行业，优先取最细一级。
    industry_boards = [item for item in ranked if int(item.get("BOARD_RANK") or 9999) <= 3]
    if industry_boards:
        item = industry_boards[-1]
        raw = str(item.get("BOARD_NAME") or "").strip()
        tag = normalize_tag(raw, aliases)
        if tag and not is_excluded(raw, tag, filters):
            score = 86 if tag not in broad else 62
            if is_specific(tag, filters):
                score += 6
            _add_candidate(candidates, tag, score, f"东方财富细分行业：{raw}")

    for item in ranked:
        if str(item.get("IS_PRECISE") or "").strip() != "1":
            continue
        raw = str(item.get("BOARD_NAME") or "").strip()
        tag = normalize_tag(raw, aliases)
        if not tag or is_excluded(raw, tag, filters):
            continue
        # 概念归属只作佐证；没有主营或细分行业支持时不单独越过72分门槛。
        score = 64
        if is_specific(tag, filters):
            score += 6
        _add_candidate(candidates, tag, score, f"东方财富业务题材：{raw}")

    min_score = float(config.get("settings", {}).get("min_score", 72))
    max_tags = int(config.get("settings", {}).get("max_tags", 3))
    eligible = sorted(
        (item for item in candidates.values() if item.score >= min_score),
        key=lambda item: (-item.score, -len(item.tag), item.tag),
    )
    deduplicated: list[TagEvidence] = []
    for item in eligible:
        if any(item.tag in kept.tag or kept.tag in item.tag for kept in deduplicated):
            continue
        deduplicated.append(item)
        if len(deduplicated) >= max_tags:
            break
    return deduplicated


def fetch_core_themes(
    code: str,
    session: requests.Session,
    *,
    retries: int = 3,
) -> list[dict[str, Any]]:
    code6 = normalize_code(code, "digits")
    params = {
        "reportName": "RPT_F10_CORETHEME_BOARDTYPE",
        "columns": (
            "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,BOARD_CODE,"
            "BOARD_NAME,IS_PRECISE,BOARD_RANK"
        ),
        "source": "WEB",
        "client": "WEB",
        "filter": f'(SECURITY_CODE="{code6}")',
        "sortColumns": "BOARD_RANK",
        "sortTypes": "1",
        "pageSize": "500",
    }
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(EASTMONEY_URL, params=params, timeout=15)
            response.raise_for_status()
            payload = response.json()
            return list((payload.get("result") or {}).get("data") or [])
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(attempt)
    raise RuntimeError(f"{code6} 题材获取失败：{last_error}")


def fetch_business_composition(
    code: str,
    session: requests.Session,
    *,
    retries: int = 3,
) -> list[dict[str, Any]]:
    market_code = normalize_code(code, "suffix")
    digits, market = market_code.split(".")
    url = "https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax"
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, params={"code": f"{market}{digits}"}, timeout=15)
            response.raise_for_status()
            return list(response.json().get("zygcfx") or [])
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(attempt)
    raise RuntimeError(f"{market_code} 主营构成获取失败：{last_error}")


def latest_raw_file(raw_dir: Path) -> Path | None:
    candidates = sorted(raw_dir.glob("eastmoney_corethemes_*.json"), reverse=True)
    return candidates[0] if candidates else None


def load_or_fetch_profiles(
    stocks: pd.DataFrame,
    *,
    raw_dir: Path,
    refresh: bool,
    offline: bool,
    interval: float,
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
    Path,
    dict[str, str],
]:
    existing = latest_raw_file(raw_dir)
    if existing and not refresh:
        payload = json.loads(existing.read_text(encoding="utf-8"))
        return (
            payload.get("profiles", {}),
            payload.get("business_profiles", {}),
            existing,
            payload.get("errors", {}),
        )
    if offline:
        raise FileNotFoundError(f"离线模式下没有可用原始题材文件：{raw_dir}")

    profiles: dict[str, list[dict[str, Any]]] = {}
    business_profiles: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/126 Safari/537.36"
            )
        }
    )
    rows = stocks[["代码", "名称"]].drop_duplicates("代码").reset_index(drop=True)
    for index, row in rows.iterrows():
        code = str(row["代码"])
        name = str(row["名称"])
        if NON_STOCK_NAME_RE.search(name):
            profiles[code] = []
            business_profiles[code] = []
            continue
        try:
            profiles[code] = fetch_core_themes(code, session)
            business_profiles[code] = fetch_business_composition(code, session)
        except Exception as exc:
            profiles.setdefault(code, [])
            business_profiles.setdefault(code, [])
            errors[code] = str(exc)
        if interval > 0:
            time.sleep(interval)
        number = index + 1
        if number % 50 == 0 or number == len(rows):
            print(f"[题材获取] {number}/{len(rows)} | 失败 {len(errors)}", flush=True)

    raw_dir.mkdir(parents=True, exist_ok=True)
    date_tag = datetime.now().strftime("%Y%m%d")
    raw_path = raw_dir / f"eastmoney_corethemes_{date_tag}.json"
    raw_payload = {
        "source": EASTMONEY_URL,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "profiles": profiles,
        "business_profiles": business_profiles,
        "errors": errors,
    }
    temp_path = raw_path.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(raw_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temp_path, raw_path)
    return profiles, business_profiles, raw_path, errors


def apply_tags(
    stocks: pd.DataFrame,
    profiles: dict[str, list[dict[str, Any]]],
    business_profiles: dict[str, list[dict[str, Any]]],
    config: dict[str, Any],
    *,
    tag_date: str,
    errors: dict[str, str] | None = None,
) -> pd.DataFrame:
    result = stocks.copy()
    errors = errors or {}
    tag_columns: dict[str, list] = {}
    for number in range(1, int(config.get("settings", {}).get("max_tags", 3)) + 1):
        tag_columns[f"标签{number}"] = []
        tag_columns[f"标签{number}相关度"] = []
        tag_columns[f"标签{number}依据"] = []
    statuses: list[str] = []

    for _, row in result.iterrows():
        code = str(row.get("代码", ""))
        name = str(row.get("名称", ""))
        if NON_STOCK_NAME_RE.search(name):
            tags: list[TagEvidence] = []
            status = "非个股"
        else:
            tags = score_tags(
                str(row.get("所属行业", "")),
                profiles.get(code, []),
                config,
                business_profiles.get(code, []),
            )
            if code in errors:
                status = "数据源失败-仅用本地行业" if tags else "数据源失败"
            else:
                status = "已完成" if tags else "无高置信标签"
        max_tags = int(config.get("settings", {}).get("max_tags", 3))
        for number in range(1, max_tags + 1):
            item = tags[number - 1] if number <= len(tags) else None
            tag_columns[f"标签{number}"].append(item.tag if item else "")
            tag_columns[f"标签{number}相关度"].append(round(item.score, 1) if item else "")
            tag_columns[f"标签{number}依据"].append("；".join(item.evidence) if item else "")
        statuses.append(status)

    for column, values in tag_columns.items():
        result[column] = values
    result["标签更新时间"] = tag_date
    result["标签状态"] = statuses
    return result


def select_output_columns(tagged: pd.DataFrame) -> list[str]:
    """产业标签结果只保留股票身份字段和标签相关字段。"""
    identity_columns = [column for column in ("代码", "名称", "市场") if column in tagged.columns]
    tag_columns = [column for column in tagged.columns if column.startswith("标签")]
    return identity_columns + tag_columns


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="为沪深股票池生成最多三个细分产业标签")
    parser.add_argument("--input", help="股票池CSV；默认使用统一配置")
    parser.add_argument("--config", default=str(DEFAULT_TAG_CONFIG), help="标签规则TOML")
    parser.add_argument(
        "--output-dir", default=config_value("files", "output_dir", "data/output")
    )
    parser.add_argument(
        "--raw-dir", default="data/history/company_profiles", help="公开题材原始数据目录"
    )
    parser.add_argument("--refresh", action="store_true", help="忽略已有原始数据并重新联网获取")
    parser.add_argument("--offline", action="store_true", help="只使用已有原始数据")
    parser.add_argument("--interval", type=float, default=0.08, help="每次公开接口请求间隔秒数")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.interval < 0:
        raise ValueError("interval不能为负数")
    input_file = resolve_input(args.input, config_key="stock_pool")
    stocks = read_csv_auto(input_file, dtype=str)
    for required in ("代码", "名称", "所属行业"):
        if required not in stocks.columns:
            raise ValueError(f"输入文件缺少【{required}】列：{input_file}")
    config = load_tag_config(args.config)
    profiles, business_profiles, raw_path, errors = load_or_fetch_profiles(
        stocks,
        raw_dir=project_path(args.raw_dir),
        refresh=args.refresh,
        offline=args.offline,
        interval=args.interval,
    )
    tag_date = datetime.now().strftime("%Y%m%d")
    tagged = apply_tags(
        stocks,
        profiles,
        business_profiles,
        config,
        tag_date=tag_date,
        errors=errors,
    )
    output_dir = project_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = dated_output_path(output_dir, "沪深_产业标签", date_tag=tag_date)
    output_columns = select_output_columns(tagged)
    write_csv(tagged[output_columns], output_path)

    audit_path = dated_output_path(output_dir, "沪深_产业标签_审计", date_tag=tag_date)
    write_csv(tagged[output_columns], audit_path)
    counts = tagged["标签状态"].value_counts(dropna=False)
    print(f"原始题材：{raw_path}")
    print(f"标签结果：{output_path}")
    print(f"审计结果：{audit_path}")
    print(f"公开数据失败：{len(errors)}")
    print(counts.to_string())
    return 1 if len(errors) == len(stocks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
