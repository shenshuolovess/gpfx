"""使用OpenAI兼容大模型对每只股票最新研报生成正文级深度总结。"""

from __future__ import annotations

import argparse
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from fetch_stock_research import ContextParagraphParser, request_response
from pipeline_config import PROJECT_DIR, project_path
from stock_utils import normalize_code


def latest_report_snapshot() -> Path:
    paths = list((PROJECT_DIR / "data/history/research_reports").glob("eastmoney_stock_reports_*.json"))
    if not paths:
        raise FileNotFoundError("没有研报快照，请先运行 fetch_stock_research.py")
    return max(paths, key=lambda path: path.stat().st_mtime_ns)


def endpoint_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith("/chat/completions") else f"{base}/chat/completions"


def effective_length(payload: dict[str, Any]) -> int:
    texts = [str(payload.get("overview") or ""), str(payload.get("consensus") or ""),
             str(payload.get("differences") or ""), str(payload.get("risks") or "")]
    texts.extend(str(item.get("analysis") or "") for item in payload.get("report_analyses", []))
    return len(re.sub(r"\s+", "", "".join(texts)))


def validate_summary(payload: dict[str, Any], reports: list[dict[str, Any]]) -> dict[str, Any]:
    required_text = ("overview", "consensus", "differences", "risks")
    for field in required_text:
        if not str(payload.get(field) or "").strip():
            raise ValueError(f"缺少字段：{field}")
    analyses = payload.get("report_analyses")
    if not isinstance(analyses, list) or len(analyses) != len(reports):
        raise ValueError("没有逐一覆盖全部研报")
    expected = [str(item.get("info_code") or "") for item in reports]
    actual = [str(item.get("info_code") or "") for item in analyses]
    if actual != expected or len(set(actual)) != len(actual):
        raise ValueError("研报编号顺序、遗漏或重复不符合要求")
    for item in analyses:
        if len(re.sub(r"\s+", "", str(item.get("analysis") or ""))) < 120:
            raise ValueError(f"逐份分析过短：{item.get('info_code')}")
    length = effective_length(payload)
    if length < 800:
        raise ValueError(f"有效总结不足800字：{length}")
    if length > 1800:
        raise ValueError(f"有效总结超过1800字：{length}")
    payload["effective_length"] = length
    return payload


def report_material(report: dict[str, Any]) -> dict[str, Any]:
    url = str(report.get("detail_url") or "")
    paragraphs: list[str] = []
    if url:
        response = request_response(url)
        response.encoding = response.apparent_encoding or "utf-8"
        parser = ContextParagraphParser()
        parser.feed(response.text)
        paragraphs = parser.paragraphs
    return {
        "info_code": report.get("info_code"), "title": report.get("title"),
        "publish_date": report.get("publish_date"), "organization": report.get("organization"),
        "researcher": report.get("researcher"), "rating": report.get("rating"),
        "body": "\n".join(paragraphs)[:9000],
    }


SYSTEM_PROMPT = """你是一名谨慎的A股研报编辑。请基于提供的最近个股研报正文，生成高信息密度的中文综合总结。
必须遵守：
1. 只能使用给定正文，不得补充新闻、常识或自行预测；所有预测必须明确归属于发布机构。
2. 逐一分析每份研报，不得遗漏，report_analyses顺序和info_code必须与输入一致。
3. 重点覆盖业绩事实及同比环比变化、业务驱动、产品或产能进展、盈利预测、估值与评级、风险。
4. overview说明材料范围与整体观点；consensus写共识；differences写假设、预测或侧重点差异；risks合并但不淡化风险。
5. 不得用免责声明、空泛评价和重复表述凑字数。有效正文不少于800字、不超过1800字，每份analysis不少于120字。
6. 不给出面向读者的买卖建议，不把券商评级写成本系统建议。
7. 输出纯JSON：{"overview":"","report_analyses":[{"info_code":"","analysis":""}],"consensus":"","differences":"","risks":""}
"""


def call_model(materials: list[dict[str, Any]]) -> dict[str, Any]:
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    model = os.environ.get("LLM_MODEL") or ""
    base_url = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    if not api_key or not model:
        raise RuntimeError("未配置 LLM_API_KEY（或 OPENAI_API_KEY）和 LLM_MODEL")
    response = requests.post(
        endpoint_url(base_url),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model, "temperature": 0.1, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": json.dumps(materials, ensure_ascii=False)}],
        }, timeout=180,
    )
    response.raise_for_status()
    raw = response.json()
    content = raw["choices"][0]["message"]["content"].strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I)
    return json.loads(content)


def summarize_one(code: str, record: dict[str, Any]) -> dict[str, Any]:
    reports = list(record.get("reports") or [])[:3]
    materials = [report_material(item) for item in reports]
    summary = validate_summary(call_model(materials), reports)
    return {"reports_used": [item.get("info_code") for item in reports], "summary": summary}


def atomic_save(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="批量生成最新三份研报的深度模型总结")
    parser.add_argument("--code", help="只处理一只股票")
    parser.add_argument("--all", action="store_true", help="处理快照中的全部股票")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--output-dir", default="data/history/research_summaries")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.all and not args.code:
        raise ValueError("请提供 --code 或 --all")
    source_path = latest_report_snapshot()
    source = json.loads(source_path.read_text(encoding="utf-8"))
    output_dir = project_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"llm_stock_research_summaries_{datetime.now():%Y%m%d}.json"
    payload = json.loads(output_path.read_text(encoding="utf-8")) if output_path.exists() else {
        "source": str(source_path), "generated_at": "", "records": {}, "errors": {}
    }
    targets = source.get("records", {})
    if args.code:
        code = normalize_code(args.code, "suffix")
        targets = {code: targets[code]}
    targets = {code: record for code, record in targets.items() if record.get("reports")}
    if not args.force:
        targets = {code: record for code, record in targets.items() if code not in payload["records"]}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        future_map = {executor.submit(summarize_one, code, record): code for code, record in targets.items()}
        for completed, future in enumerate(as_completed(future_map), start=1):
            code = future_map[future]
            try:
                payload["records"][code] = future.result()
                payload["errors"].pop(code, None)
            except Exception as exc:
                payload["errors"][code] = str(exc)
            payload["generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            if completed % 5 == 0 or completed == len(targets):
                atomic_save(output_path, payload)
                print(f"[深度研报总结] {completed}/{len(targets)} | 成功 {len(payload['records'])} | 失败 {len(payload['errors'])}", flush=True)
    print(f"总结快照：{output_path}")
    return 1 if payload["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
