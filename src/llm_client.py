"""OpenAI兼容Chat Completions接口的轻量适配层。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import requests

from brief_schema import validate_analysis


FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def llm_configured() -> bool:
    return bool(os.environ.get("LLM_API_KEY") and os.environ.get("LLM_MODEL"))


def endpoint_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith("/chat/completions") else f"{base}/chat/completions"


def parse_json_content(content: str) -> dict[str, Any]:
    cleaned = FENCE_RE.sub("", content.strip()).strip()
    return json.loads(cleaned)


def compact_evidence_for_llm(evidence: dict[str, Any]) -> dict[str, Any]:
    """模型只接收可进入主结论的组，完整证据仍保存在本地。"""
    fields = (
        "id", "name", "kind", "sample_count", "daily_return_median", "up_ratio",
        "return_5d_median", "return_20d_median", "return_60d_median",
        "trend_score_median", "rs_score_median", "position_score_median",
        "exhaustion_score_median", "strong_state_ratio", "weak_state_ratio",
        "became_strong_count", "became_weak_count", "pe_median",
        "revenue_yoy_median", "profit_yoy_median", "average_relevance",
        "roic_median", "board", "positive_signals", "negative_signals",
        "positive_signal_count", "negative_signal_count", "signal_balance",
        "benchmark_name", "excess_daily_return", "excess_5d_return",
        "excess_20d_return", "excess_60d_return",
        "financial_coverage_count", "financial_coverage_ratio",
        "single_stock_influence",
    )

    def compact_group(item: dict[str, Any]) -> dict[str, Any]:
        result = {field: item.get(field) for field in fields if field in item}
        result["representatives"] = [
            {"name": row.get("name"), "daily_return": row.get("daily_return")}
            for row in item.get("leaders", [])[:2]
        ]
        result["main_stocks"] = [
            {"code": row.get("code"), "name": row.get("name")}
            for row in item.get("main_stocks", [])[:8]
        ]
        return result

    return {
        "schema_version": evidence.get("schema_version"),
        "as_of": evidence.get("as_of"),
        "scope_note": evidence.get("scope_note"),
        "market": evidence.get("market"),
        "benchmarks": evidence.get("benchmarks"),
        "industries": [
            compact_group(item)
            for item in evidence.get("industries", [])
            if item.get("sample_count", 0) > 3
        ],
        "tags": [
            compact_group(item)
            for item in evidence.get("tags", [])
            if item.get("sample_count", 0) > 3
        ],
        "rankings": evidence.get("rankings"),
    }


def call_llm(
    evidence: dict[str, Any],
    prompt_path: Path,
    *,
    timeout: int = 90,
) -> tuple[dict[str, Any], dict[str, Any]]:
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")
    base_url = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    if not api_key or not model:
        raise RuntimeError("未配置 LLM_API_KEY 或 LLM_MODEL")
    system_prompt = prompt_path.read_text(encoding="utf-8")
    compact_evidence = json.dumps(
        compact_evidence_for_llm(evidence), ensure_ascii=False, separators=(",", ":")
    )
    payload = {
        "model": model,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"以下是唯一允许使用的证据包：\n{compact_evidence}"},
        ],
    }
    response = requests.post(
        endpoint_url(base_url),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    raw = response.json()
    content = raw["choices"][0]["message"]["content"]
    analysis = validate_analysis(parse_json_content(content), evidence)
    metadata = {
        "provider_endpoint": endpoint_url(base_url),
        "model": model,
        "usage": raw.get("usage") or {},
        "response_id": raw.get("id") or "",
    }
    return analysis, metadata
