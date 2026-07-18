"""批量持久化东方财富个股最新研报及正文中的观点标题。"""

from __future__ import annotations

import argparse
import html
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests

from fetch_company_financials import is_index
from pipeline_config import project_path, resolve_input
from stock_utils import normalize_code, read_csv_auto


LIST_URL = "https://reportapi.eastmoney.com/report/list"
DETAIL_URL = "https://data.eastmoney.com/report/info/{info_code}.html"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Referer": "https://data.eastmoney.com/report/stock.jshtml",
}
GENERIC_HEADINGS = {
    "事件", "投资要点", "经营分析", "投资建议", "盈利预测", "估值与评级",
    "盈利预测、估值与评级", "公司简介", "报告摘要", "核心观点", "主要观点",
}
RATING_CHANGE_NAMES = {1: "调高", 2: "首次", 3: "维持", 4: "调低", 5: "无变化"}


class ContextParagraphParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.context_depth = 0
        self.in_paragraph = False
        self.buffer: list[str] = []
        self.paragraphs: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if tag == "div" and attributes.get("id") == "ctx-content":
            self.context_depth = 1
        elif self.context_depth and tag == "div":
            self.context_depth += 1
        if self.context_depth and tag == "p":
            self.in_paragraph = True
            self.buffer = []

    def handle_endtag(self, tag: str) -> None:
        if self.context_depth and tag == "p" and self.in_paragraph:
            text = "".join(self.buffer).replace("\u3000", " ").strip()
            if text:
                self.paragraphs.append(" ".join(text.split()))
            self.in_paragraph = False
        if self.context_depth and tag == "div":
            self.context_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.in_paragraph:
            self.buffer.append(data)


def request_response(url: str, *, params: dict[str, str] | None = None, retries: int = 3) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, params=params, headers=HEADERS, timeout=25)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(attempt)
    raise RuntimeError(str(last_error))


def extract_detail(html_text: str, fallback_title: str) -> dict[str, Any]:
    parser = ContextParagraphParser()
    parser.feed(html_text)
    paragraphs = [html.unescape(item) for item in parser.paragraphs]
    risk = next((item for item in paragraphs if item.startswith(("风险提示", "风险因素"))), "")
    viewpoints = [
        item for item in paragraphs
        if len(item) <= 90
        and not item.endswith(("。", "；", ";"))
        and item.strip("：: ") not in GENERIC_HEADINGS
        and not item.startswith(("风险提示", "风险因素"))
        and not ("(" in item and ")" in item and any(char.isdigit() for char in item))
    ][:3]
    if not viewpoints:
        viewpoints = [fallback_title] if fallback_title else []
    if risk.strip("：: ") in {"风险提示", "风险因素"}:
        risk = ""
    return {"viewpoints": viewpoints, "risk": risk}


def fetch_one(code: str, limit: int = 3, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    digits = normalize_code(code, "suffix").split(".")[0]
    today = datetime.now().strftime("%Y-%m-%d")
    payload = request_response(
        LIST_URL,
        params={
            "industryCode": "*", "pageSize": str(limit), "industry": "*", "rating": "*",
            "ratingChange": "*", "beginTime": "2020-01-01", "endTime": today,
            "pageNo": "1", "fields": "", "qType": "0", "orgCode": "", "code": digits,
            "rcode": "", "p": "1", "pageNum": "1", "pageNumber": "1",
        },
    ).json()
    reports = []
    previous_by_info = {
        str(report.get("info_code") or ""): report
        for report in (previous or {}).get("reports", [])
        if report.get("info_code")
    }
    base_year = int(payload.get("currentYear") or datetime.now().year)
    for item in (payload.get("data") or [])[:limit]:
        info_code = str(item.get("infoCode") or "")
        detail = {"viewpoints": [str(item.get("title") or "")], "risk": ""}
        detail_error = ""
        if info_code in previous_by_info:
            cached = previous_by_info[info_code]
            detail = {
                "viewpoints": list(cached.get("viewpoints") or detail["viewpoints"]),
                "risk": str(cached.get("risk") or ""),
            }
            detail_error = str(cached.get("detail_error") or "")
        elif info_code:
            try:
                response = request_response(DETAIL_URL.format(info_code=info_code))
                response.encoding = response.apparent_encoding or "utf-8"
                detail = extract_detail(response.text, str(item.get("title") or ""))
            except Exception as exc:
                detail_error = str(exc)
        reports.append(
            {
                "info_code": info_code,
                "title": str(item.get("title") or ""),
                "publish_date": str(item.get("publishDate") or "")[:10],
                "organization": str(item.get("orgSName") or item.get("orgName") or ""),
                "researcher": str(item.get("researcher") or ""),
                "rating": str(item.get("emRatingName") or ""),
                "rating_change": item.get("ratingChange"),
                "rating_change_name": RATING_CHANGE_NAMES.get(item.get("ratingChange"), "未标注"),
                "forecasts": [
                    {"year": base_year, "eps": item.get("predictThisYearEps"), "pe": item.get("predictThisYearPe")},
                    {"year": base_year + 1, "eps": item.get("predictNextYearEps"), "pe": item.get("predictNextYearPe")},
                    {"year": base_year + 2, "eps": item.get("predictNextTwoYearEps"), "pe": item.get("predictNextTwoYearPe")},
                ],
                "target_price_low": item.get("indvAimPriceL"),
                "target_price_high": item.get("indvAimPriceT"),
                "viewpoints": detail["viewpoints"],
                "risk": detail["risk"],
                "detail_url": DETAIL_URL.format(info_code=info_code) if info_code else "",
                "detail_error": detail_error,
            }
        )
    return {"report_count": int(payload.get("hits") or 0), "reports": reports}


def fetch_all(stocks, workers: int = 8, limit: int = 3, previous_records: dict[str, Any] | None = None):
    records: dict[str, Any] = {}
    errors: dict[str, str] = {}
    skipped = []
    targets = []
    for _, row in stocks[["代码", "名称"]].drop_duplicates("代码").iterrows():
        code = normalize_code(row["代码"], "suffix")
        name = str(row["名称"]).strip()
        if is_index(code, name):
            skipped.append({"code": code, "name": name, "reason": "非个股"})
        else:
            targets.append((code, name))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        future_map = {
            executor.submit(fetch_one, code, limit, (previous_records or {}).get(code)): (code, name)
            for code, name in targets
        }
        for completed, future in enumerate(as_completed(future_map), start=1):
            code, _ = future_map[future]
            try:
                records[code] = future.result()
            except Exception as exc:
                errors[code] = str(exc)
            if completed % 25 == 0 or completed == len(targets):
                reports = sum(len(item.get("reports", [])) for item in records.values())
                print(f"[研报资料] {completed}/{len(targets)} | 股票 {len(records)} | 研报 {reports} | 失败 {len(errors)}", flush=True)
    return records, errors, skipped


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="更新股票最新研报持久化快照")
    parser.add_argument("--input", help="股票池CSV；默认使用统一配置中的沪深.csv")
    parser.add_argument("--output-dir", default="data/history/research_reports")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    input_path = resolve_input(args.input, config_key="stock_pool")
    output_dir = project_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    date_tag = datetime.now().strftime("%Y%m%d")
    output_path = output_dir / f"eastmoney_stock_reports_{date_tag}.json"
    if output_path.exists() and not args.force:
        print(f"当天快照已存在：{output_path}")
        return 0
    stocks = read_csv_auto(input_path, dtype=str)
    previous_records: dict[str, Any] = {}
    if output_path.exists():
        try:
            previous_records = json.loads(output_path.read_text(encoding="utf-8")).get("records", {})
            print(f"复用已有正文：{len(previous_records)} 只股票", flush=True)
        except (OSError, json.JSONDecodeError):
            previous_records = {}
    records, errors, skipped = fetch_all(
        stocks, args.workers, max(1, min(args.limit, 3)), previous_records
    )
    payload = {
        "source": {"list": LIST_URL, "detail": DETAIL_URL},
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "input": str(input_path), "limit": args.limit,
        "records": records, "skipped": skipped, "errors": errors,
    }
    temp_path = output_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temp_path, output_path)
    report_count = sum(len(item.get("reports", [])) for item in records.values())
    print(f"快照：{output_path}\n股票：{len(records)}；研报：{report_count}；跳过：{len(skipped)}；失败：{len(errors)}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
