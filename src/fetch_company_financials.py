"""批量持久化东方财富F10公司概况与主要财务指标。"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from pipeline_config import project_path, resolve_input
from stock_utils import normalize_code, read_csv_auto


BASE_URL = "https://emweb.securities.eastmoney.com/PC_HSF10"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/126 Safari/537.36"
    )
}
KNOWN_INDEX_CODES = {
    "000001.SH", "000016.SH", "000300.SH", "000688.SH", "000905.SH",
    "399001.SZ", "399006.SZ",
}


def is_index(code: str, name: str) -> bool:
    return (
        normalize_code(code, "suffix") in KNOWN_INDEX_CODES
        or name.endswith("指数")
        or name in {"上证50", "沪深300", "中证500", "科创50", "深证成指", "创业板指"}
    )


def request_json(url: str, params: dict[str, str], retries: int = 3) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, params=params, headers=HEADERS, timeout=25)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(attempt)
    raise RuntimeError(str(last_error))


def fetch_one(code: str) -> dict[str, Any]:
    suffix_code = normalize_code(code, "suffix")
    digits, market = suffix_code.split(".")
    api_code = f"{market}{digits}"
    survey = request_json(
        f"{BASE_URL}/CompanySurvey/PageAjax",
        {"code": api_code},
    )
    finance = request_json(
        f"{BASE_URL}/NewFinanceAnalysis/ZYZBAjaxNew",
        {"type": "0", "code": api_code},
    )
    return {
        "company": (survey.get("jbzl") or [{}])[0],
        "listing": (survey.get("fxxg") or [{}])[0],
        "financials": list(finance.get("data") or []),
    }


def fetch_all(stocks, workers: int = 8) -> tuple[dict[str, Any], dict[str, str], list[dict[str, str]]]:
    records: dict[str, Any] = {}
    errors: dict[str, str] = {}
    skipped: list[dict[str, str]] = []
    targets: list[tuple[str, str]] = []
    for _, row in stocks[["代码", "名称"]].drop_duplicates("代码").iterrows():
        code = normalize_code(row["代码"], "suffix")
        name = str(row["名称"]).strip()
        if is_index(code, name):
            skipped.append({"code": code, "name": name, "reason": "非个股"})
        else:
            targets.append((code, name))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        future_map = {executor.submit(fetch_one, code): (code, name) for code, name in targets}
        completed = 0
        for future in as_completed(future_map):
            code, _ = future_map[future]
            completed += 1
            try:
                records[code] = future.result()
            except Exception as exc:
                errors[code] = str(exc)
            if completed % 25 == 0 or completed == len(targets):
                print(
                    f"[公司财务资料] {completed}/{len(targets)} | "
                    f"成功 {len(records)} | 失败 {len(errors)}",
                    flush=True,
                )
    return records, errors, skipped


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="更新公司概况与主要财务指标持久化快照")
    parser.add_argument("--input", help="股票池CSV；默认使用统一配置中的沪深.csv")
    parser.add_argument("--output-dir", default="data/history/company_financials")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--force", action="store_true", help="覆盖当天已有快照")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    input_path = resolve_input(args.input, config_key="stock_pool")
    output_dir = project_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    date_tag = datetime.now().strftime("%Y%m%d")
    output_path = output_dir / f"eastmoney_company_financials_{date_tag}.json"
    if output_path.exists() and not args.force:
        print(f"当天快照已存在：{output_path}")
        return 0

    stocks = read_csv_auto(input_path, dtype=str)
    records, errors, skipped = fetch_all(stocks, workers=args.workers)
    payload = {
        "source": {
            "company": f"{BASE_URL}/CompanySurvey/PageAjax",
            "financials": f"{BASE_URL}/NewFinanceAnalysis/ZYZBAjaxNew",
        },
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "input": str(input_path),
        "records": records,
        "skipped": skipped,
        "errors": errors,
    }
    temp_path = output_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temp_path, output_path)
    print(f"快照：{output_path}")
    print(f"成功：{len(records)}；跳过：{len(skipped)}；失败：{len(errors)}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
