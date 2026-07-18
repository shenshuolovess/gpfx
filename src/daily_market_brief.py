"""生成以行业和产业标签为核心的沪深股票池当日分析报告。"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from brief_renderer import render_html, render_markdown
from brief_schema import deterministic_analysis
from llm_client import call_llm, llm_configured
from market_evidence import build_evidence
from pipeline_config import project_path


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    os.replace(temp, path)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="生成沪深股票池行业与产业标签日报")
    parser.add_argument("--date", help="指定YYYYMMDD；默认取最新分类日期")
    parser.add_argument("--no-llm", action="store_true", help="不调用大模型，使用确定性归纳")
    parser.add_argument("--strict-llm", action="store_true", help="模型失败时直接报错，不降级")
    parser.add_argument("--evidence-only", action="store_true", help="只生成证据包")
    parser.add_argument("--history-dir", default="data/history/daily_briefs")
    parser.add_argument("--output-dir", default="data/output/daily_briefs")
    parser.add_argument("--prompt", default="prompts/daily_market_brief.md")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    evidence = build_evidence(args.date)
    date_tag = evidence["as_of"]
    history_dir = project_path(args.history_dir) / date_tag
    output_dir = project_path(args.output_dir)
    evidence_path = history_dir / "evidence.json"
    write_json(evidence_path, evidence)
    if args.evidence_only:
        print(f"证据包：{evidence_path}")
        return 0

    metadata: dict[str, Any] = {}
    mode = "确定性统计版"
    if not args.no_llm and llm_configured():
        try:
            analysis, metadata = call_llm(evidence, project_path(args.prompt))
            mode = f"大模型归纳 · {metadata['model']}"
        except Exception as exc:
            if args.strict_llm:
                raise
            analysis = deterministic_analysis(evidence)
            metadata = {"fallback_reason": str(exc)}
            mode = "确定性统计版（模型调用失败后降级）"
    else:
        analysis = deterministic_analysis(evidence)
        if not args.no_llm:
            metadata = {"fallback_reason": "未配置 LLM_API_KEY 或 LLM_MODEL"}
            mode = "确定性统计版（未配置模型）"

    analysis_path = history_dir / "llm_analysis.json"
    write_json(analysis_path, analysis)
    manifest = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "as_of": date_tag,
        "mode": mode,
        "sources": evidence["sources"],
        "model": metadata,
        "evidence": str(evidence_path),
        "analysis": str(analysis_path),
    }
    write_json(history_dir / "run_manifest.json", manifest)

    markdown_path = output_dir / f"沪深行业标签日报_{date_tag}.md"
    html_path = output_dir / f"沪深行业标签日报_{date_tag}.html"
    atomic_write(markdown_path, render_markdown(evidence, analysis, mode))
    atomic_write(html_path, render_html(evidence, analysis, mode))
    print(f"模式：{mode}")
    print(f"证据包：{evidence_path}")
    print(f"Markdown：{markdown_path}")
    print(f"HTML：{html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
