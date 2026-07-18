"""将证据与模型选择渲染为Markdown和离线HTML日报。"""

from __future__ import annotations

import html
from typing import Any

from brief_schema import evidence_index


SECTION_TITLES = {
    "industry_view": "全部合格行业分析",
    "tag_view": "全部合格产业标签分析",
    "turning_points": "转强与转弱",
    "risks": "风险观察",
}


def pct(value: Any, digits: int = 2, signed: bool = False) -> str:
    if value is None:
        return "—"
    sign = "+" if signed and value > 0 else ""
    return f"{sign}{value:.{digits}f}%"


def ratio_pct(value: Any) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def group_metrics(group: dict[str, Any]) -> str:
    pieces = [
        f"样本 {group['sample_count']}只",
        f"今日中位 {pct(group.get('daily_return_median'), signed=True)}",
        f"上涨率 {ratio_pct(group.get('up_ratio'))}",
        f"20日中位 {pct(group.get('return_20d_median'), signed=True)}",
        f"强势分类占比 {ratio_pct(group.get('strong_state_ratio'))}",
    ]
    if group.get("benchmark_name") and group.get("excess_daily_return") is not None:
        pieces.append(f"较{group['benchmark_name']}超额 {pct(group['excess_daily_return'], signed=True)}")
    return "｜".join(pieces)


def fundamental_context(group: dict[str, Any]) -> str:
    values = []
    if group.get("revenue_yoy_median") is not None:
        values.append(f"营收同比中位 {pct(group['revenue_yoy_median'], signed=True)}")
    if group.get("profit_yoy_median") is not None:
        values.append(f"净利同比中位 {pct(group['profit_yoy_median'], signed=True)}")
    if group.get("pe_median") is not None:
        values.append(f"PE中位 {group['pe_median']:.1f}倍")
    return "｜".join(values)


def representative_text(group: dict[str, Any]) -> str:
    leaders = "、".join(
        f"{item['name']}({pct(item.get('daily_return'), signed=True)})"
        for item in group.get("leaders", [])
    )
    return leaders or "—"


def market_cap_text(value: Any) -> str:
    if value is None:
        return "市值—"
    if abs(value) >= 100_000_000:
        return f"市值{value / 100_000_000:,.1f}亿"
    if abs(value) >= 10_000:
        return f"市值{value / 10_000:,.1f}万"
    return f"市值{value:,.0f}"


def main_stocks_text(group: dict[str, Any]) -> str:
    stocks = [main_stock_item_text(item) for item in group.get("main_stocks", [])]
    return "、".join(stocks) or "—"


def main_stock_item_text(item: dict[str, Any]) -> str:
    details = [market_cap_text(item.get("market_cap")), pct(item.get("daily_return"), signed=True)]
    if item.get("classification"):
        details.append(item["classification"])
    if item.get("relevance") is not None:
        details.append(f"相关度{item['relevance']:.0f}")
    return f"{item['name']}({item['code']}；{'；'.join(details)})"


def data_quality_text(group: dict[str, Any]) -> str:
    sample_count = group.get("sample_count", 0)
    coverage_count = group.get("financial_coverage_count", 0)
    coverage_ratio = group.get("financial_coverage_ratio")
    relevance = group.get("average_relevance")
    relevance_text = f"{relevance:.1f}分" if relevance is not None else "不适用"
    influence = group.get("single_stock_influence") or {}
    if influence.get("is_high"):
        stock = influence.get("stock_name") or influence.get("stock_code") or "某只股票"
        reasons = "、".join(influence.get("reasons") or []) or "留一法结果变化明显"
        influence_text = f"较高（{stock}：{reasons}）"
    else:
        influence_text = "未发现明显单股影响"
    return (
        f"股票{sample_count}只｜财务覆盖{coverage_count}/{sample_count}（{ratio_pct(coverage_ratio)}）｜"
        f"标签平均相关度{relevance_text}｜单股影响{influence_text}"
    )


def stock_details_html(group: dict[str, Any]) -> str:
    stocks = "".join(
        f'<div class="stock-row">{html.escape(main_stock_item_text(item))}</div>'
        for item in group.get("main_stocks", [])
    ) or '<div class="stock-row">—</div>'
    leaders = "".join(
        f'<div class="stock-row">{html.escape(item["name"])}（{pct(item.get("daily_return"), signed=True)}）</div>'
        for item in group.get("leaders", [])
    ) or '<div class="stock-row">—</div>'
    return (
        '<details class="stock-details"><summary>展开股票明细</summary>'
        f'<div class="stock-detail-body"><h4>主要股票</h4>{stocks}'
        f'<h4>当日领涨</h4>{leaders}</div></details>'
    )


def signal_text(group: dict[str, Any], board: str) -> str:
    signals = group.get("positive_signals" if board == "red" else "negative_signals") or []
    return "、".join(signals) or "暂无足够同向信号"


def board_groups(evidence: dict[str, Any], kind: str, board: str) -> list[dict[str, Any]]:
    index = evidence_index(evidence)
    return [
        index[group_id]
        for group_id in evidence.get("rankings", {}).get(f"{kind}_{board}_board", [])
    ]


def group_names(evidence: dict[str, Any], ranking: str, limit: int = 4) -> str:
    index = evidence_index(evidence)
    names = [index[group_id]["name"] for group_id in evidence.get("rankings", {}).get(ranking, [])[:limit] if group_id in index]
    return "、".join(names) or "暂无"


def market_overall_summary(evidence: dict[str, Any]) -> str:
    """生成包含市场广度、分类迁移和结构主线的可核验整体总结。"""
    market = evidence["market"]
    stock_count = market.get("stock_count", 0)
    up_count = market.get("up_count", 0)
    down_count = market.get("down_count", 0)
    up_ratio = market.get("up_ratio")
    strong_ratio = market.get("strong_state_ratio")
    breadth = "偏强" if up_ratio is not None and up_ratio >= 0.6 else "偏弱" if up_ratio is not None and up_ratio <= 0.4 else "分化"
    migration = market.get("became_strong_count", 0) - market.get("became_weak_count", 0)
    migration_text = "净转强" if migration > 0 else "净转弱" if migration < 0 else "转强与转弱持平"

    industry_red = group_names(evidence, "industry_red_board")
    industry_black = group_names(evidence, "industry_black_board")
    tag_red = group_names(evidence, "tag_red_board")
    tag_black = group_names(evidence, "tag_black_board")
    primary = evidence.get("benchmarks", {}).get("primary") or {}
    benchmark_text = ""
    if primary:
        benchmark_text = (
            f"大盘方面，{primary.get('name')}当日{pct(primary.get('daily_return'), signed=True)}、"
            f"二十日{pct(primary.get('return_20d'), signed=True)}、六十日{pct(primary.get('return_60d'), signed=True)}，"
            f"当前分类为{primary.get('classification') or '—'}。"
        )
    return (
        f"本次统计覆盖{stock_count}只股票，上涨{up_count}只、下跌{down_count}只，"
        f"上涨比例为{ratio_pct(up_ratio)}，涨跌幅中位数为{pct(market.get('daily_return_median'), signed=True)}，市场广度整体{breadth}。"
        f"强势分类占比为{ratio_pct(strong_ratio)}；相较上一份分类结果，转强{market.get('became_strong_count', 0)}只、"
        f"转弱{market.get('became_weak_count', 0)}只，分类状态呈现{migration_text}。"
        f"{benchmark_text}"
        f"结构上，行业红榜主要为{industry_red}，承压行业主要为{industry_black}；"
        f"标签红榜集中在{tag_red}，标签黑榜集中在{tag_black}。"
    )


def render_markdown(evidence: dict[str, Any], analysis: dict[str, Any], model_label: str) -> str:
    market = evidence["market"]
    index = evidence_index(evidence)
    lines = [
        f"# 沪深股票池行业与标签日报 · {evidence['as_of']}",
        "",
        f"> {analysis['market_summary']}",
        "",
        "## 市场概览",
        "",
        f"股票 {market['stock_count']}只｜上涨 {market['up_count']}只｜下跌 {market['down_count']}只｜今日涨跌幅中位 {pct(market.get('daily_return_median'), signed=True)}｜强势分类占比 {ratio_pct(market.get('strong_state_ratio'))}",
        "",
        f"相较上一份分类结果：转强 {market['became_strong_count']}只，转弱 {market['became_weak_count']}只。",
        "",
        "## 市场整体总结",
        "",
        market_overall_summary(evidence),
    ]
    indices = evidence.get("benchmarks", {}).get("indices", [])
    lines.extend(["", "## 大盘基准", "", "| 指数 | 分类 | 当日 | 5日 | 20日 | 60日 |", "|---|---|---:|---:|---:|---:|"])
    if indices:
        for item in indices:
            lines.append(
                f"| {item.get('name') or '—'} | {item.get('classification') or '—'} | "
                f"{pct(item.get('daily_return'), signed=True)} | {pct(item.get('return_5d'), signed=True)} | "
                f"{pct(item.get('return_20d'), signed=True)} | {pct(item.get('return_60d'), signed=True)} |"
            )
    else:
        lines.append("| 暂无指数数据 | — | — | — | — | — |")
    for board, title in (("red", "红榜：多维证据偏强"), ("black", "黑榜：多维压力偏高")):
        lines.extend(["", f"## {title}", ""])
        for kind, subtitle in (("industry", "行业"), ("tag", "产业标签")):
            groups = board_groups(evidence, kind, board)
            lines.extend([f"### {subtitle}{'红榜' if board == 'red' else '黑榜'}", ""])
            if not groups:
                lines.extend(["暂无满足条件的组。", ""])
                continue
            for order, group in enumerate(groups, start=1):
                lines.append(f"{order}. **{group['name']}**｜{group_metrics(group)}")
                lines.append(f"   - 入榜依据：{signal_text(group, board)}")
                opposing = signal_text(group, "black" if board == "red" else "red")
                if opposing != "暂无足够同向信号":
                    lines.append(f"   - 反向证据：{opposing}")
                lines.append(f"   - 主要股票：{main_stocks_text(group)}")
                lines.append(f"   - 当日领涨代表：{representative_text(group)}")
                lines.append(f"   - 数据质量：{data_quality_text(group)}")
            lines.append("")
    for section, title in SECTION_TITLES.items():
        lines.extend(["", f"## {title}", ""])
        items = analysis.get(section, [])
        if not items:
            lines.append("暂无满足条件的可靠信号。")
            continue
        for item in items:
            groups = [index[group_id] for group_id in item["evidence_ids"]]
            lines.append(f"### {' / '.join(group['name'] for group in groups)}")
            lines.append("")
            lines.append(item["interpretation"])
            lines.append("")
            for group in groups:
                lines.append(f"- `{group['id']}`：{group_metrics(group)}")
                context = fundamental_context(group)
                if context:
                    lines.append(f"- 基本面背景：{context}")
                lines.append(f"- 主要股票：{main_stocks_text(group)}")
                lines.append(f"- 当日领涨代表：{representative_text(group)}")
                lines.append(f"- 数据质量：{data_quality_text(group)}")
            if item.get("caveat"):
                lines.append(f"- 注意：{item['caveat']}")
            lines.append("")
    lines.extend(
        [
            "## 数据说明",
            "",
            f"- {evidence['scope_note']}",
            f"- 报告模式：{model_label}",
            f"- 分类文件：`{evidence['sources']['classification']}`",
            f"- 标签文件：`{evidence['sources']['tags']}`",
            "- 数字、排名和代表股票均由代码生成；大模型仅参与证据选择与文字归纳。",
            "- 本报告用于研究记录，不构成投资建议。",
            "",
        ]
    )
    return "\n".join(lines)


def render_html(evidence: dict[str, Any], analysis: dict[str, Any], model_label: str) -> str:
    market = evidence["market"]
    index = evidence_index(evidence)
    sections = []
    for section, title in SECTION_TITLES.items():
        cards = []
        for item in analysis.get(section, []):
            groups = [index[group_id] for group_id in item["evidence_ids"]]
            evidence_html = "".join(
                f"<li><b>{html.escape(group['name'])}</b><span>{html.escape(group_metrics(group))}</span>"
                f"{stock_details_html(group)}<small class=\"quality\">数据质量：{html.escape(data_quality_text(group))}</small></li>"
                for group in groups
            )
            caveat = f'<p class="caveat">{html.escape(item["caveat"])}</p>' if item.get("caveat") else ""
            cards.append(
                f'<article><h3>{html.escape(" / ".join(group["name"] for group in groups))}</h3>'
                f'<p>{html.escape(item["interpretation"])}</p><ul>{evidence_html}</ul>{caveat}</article>'
            )
        body = "".join(cards) or '<p class="empty">暂无满足条件的可靠信号。</p>'
        sections.append(f'<section><div class="section-title"><h2>{title}</h2></div><div class="cards">{body}</div></section>')
    board_sections = []
    for board, title in (("red", "红榜 · 多维证据偏强"), ("black", "黑榜 · 多维压力偏高")):
        columns = []
        for kind, subtitle in (("industry", "行业"), ("tag", "产业标签")):
            rows = "".join(
                f'<li><b>{html.escape(group["name"])}</b><span>{html.escape(group_metrics(group))}</span>'
                f'<small>依据：{html.escape(signal_text(group, board))}</small>{stock_details_html(group)}'
                f'<small class="quality">数据质量：{html.escape(data_quality_text(group))}</small></li>'
                for group in board_groups(evidence, kind, board)
            ) or '<p class="empty">暂无满足条件的组。</p>'
            columns.append(f'<div class="board-column"><h3>{subtitle}</h3><ol>{rows}</ol></div>')
        board_sections.append(f'<section class="board-section {board}"><div class="section-title"><h2>{title}</h2></div><div class="board-grid">{"".join(columns)}</div></section>')
    benchmark_rows = "".join(
        f'<tr><td><b>{html.escape(item.get("name") or "—")}</b><small>{html.escape(item.get("classification") or "—")}</small></td>'
        f'<td>{pct(item.get("daily_return"), signed=True)}</td><td>{pct(item.get("return_5d"), signed=True)}</td>'
        f'<td>{pct(item.get("return_20d"), signed=True)}</td><td>{pct(item.get("return_60d"), signed=True)}</td></tr>'
        for item in evidence.get("benchmarks", {}).get("indices", [])
    ) or '<tr><td colspan="5">暂无指数数据</td></tr>'
    benchmark_section = f'<section><div class="section-title"><h2>大盘基准</h2></div><div class="benchmark-table"><table><thead><tr><th>指数</th><th>当日</th><th>五日</th><th>二十日</th><th>六十日</th></tr></thead><tbody>{benchmark_rows}</tbody></table></div></section>'
    overall_summary = html.escape(market_overall_summary(evidence))
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>沪深行业与标签日报 {evidence['as_of']}</title><style>
    :root{{--bg:#07111d;--panel:#0e2131;--line:#203c50;--text:#edf6f7;--muted:#8da8b5;--cyan:#52e0c4;--amber:#ffc861;--red:#ff7478}}*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at 85% 0,#173d50,transparent 32%),var(--bg);color:var(--text);font-family:Inter,"PingFang SC","Microsoft YaHei",sans-serif}}main{{width:min(1120px,calc(100% - 28px));margin:auto;padding:38px 0 60px}}header{{padding:32px;border:1px solid rgba(82,224,196,.2);border-radius:22px;background:linear-gradient(145deg,#10283a,#091a29)}}.eyebrow{{font-size:10px;color:var(--cyan);letter-spacing:.18em}}h1{{font-size:clamp(32px,6vw,62px);margin:10px 0 18px;letter-spacing:-.05em}}header>p{{font-size:18px;line-height:1.75;color:#d1e1e5;max-width:850px}}.market{{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-top:24px}}.market div{{padding:13px;background:rgba(255,255,255,.04);border-radius:11px}}.market span{{display:block;color:var(--muted);font-size:9px;margin-bottom:5px}}.market b{{font-size:16px}}section{{margin-top:38px}}.section-title{{display:flex;align-items:end;justify-content:space-between;margin-bottom:14px}}h2{{font-size:24px;margin:0}}.overall-summary{{padding:24px 26px;border-radius:16px;border:1px solid rgba(82,224,196,.18);background:linear-gradient(145deg,rgba(16,35,53,.96),rgba(10,26,40,.96))}}.overall-summary p{{margin:0;color:#d6e5e8;font-size:15px;line-height:1.9}}.benchmark-table{{overflow-x:auto;border:1px solid rgba(82,224,196,.14);border-radius:14px;background:rgba(255,255,255,.025)}}table{{width:100%;border-collapse:collapse;min-width:620px}}th,td{{padding:13px 16px;text-align:right;border-bottom:1px solid rgba(255,255,255,.07);font-size:12px}}th{{color:var(--muted);font-size:10px}}th:first-child,td:first-child{{text-align:left}}td small{{display:block;color:var(--muted);margin-top:3px}}.cards{{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}}article{{background:linear-gradient(145deg,#102335,#0a1a28);border:1px solid rgba(128,181,201,.14);border-radius:16px;padding:20px}}h3{{margin:0 0 10px;color:var(--cyan);font-size:18px}}article>p{{font-size:13px;line-height:1.7;color:#cad9de}}ul{{list-style:none;padding:0;margin:15px 0 0}}li{{padding:11px 0;border-top:1px solid rgba(255,255,255,.07)}}li b,li span,li small{{display:block}}li span{{font-size:11px;color:var(--muted);line-height:1.6;margin-top:5px}}li small{{font-size:10px;color:#6f909e;margin-top:4px}}li small.quality{{margin-top:8px;padding:7px 9px;border-radius:8px;background:rgba(255,200,97,.07);color:#d8bd80}}.stock-details{{margin-top:8px;border:1px solid rgba(82,224,196,.13);border-radius:8px;background:rgba(82,224,196,.035)}}.stock-details summary{{padding:8px 10px;cursor:pointer;color:#8fcfc4;font-size:10px;user-select:none}}.stock-details summary:hover{{color:var(--cyan)}}.stock-details[open] summary{{border-bottom:1px solid rgba(82,224,196,.1)}}.stock-details .stock-detail-body{{padding:6px 10px 10px}}.stock-details h4{{margin:8px 0 4px;color:#b8d1d7;font-size:10px}}.stock-row{{padding:5px 0!important;border-top:1px dashed rgba(255,255,255,.06);color:#7898a5;font-size:10px;line-height:1.55;overflow-wrap:anywhere}}.stock-details h4+.stock-row{{border-top:0}}.board-section{{padding:22px;border-radius:18px;border:1px solid rgba(255,255,255,.09);background:rgba(255,255,255,.02)}}.board-section.red{{border-color:rgba(255,116,120,.24)}}.board-section.red h2,.board-section.red h3{{color:var(--red)}}.board-section.black{{border-color:rgba(175,140,255,.22)}}.board-section.black h2,.board-section.black h3{{color:#bd9cff}}.board-grid{{display:grid;grid-template-columns:1fr 1fr;gap:22px}}.board-column ol{{padding-left:20px;margin:0}}.board-column li{{padding-left:6px}}.caveat{{color:var(--amber);font-size:10px;margin-bottom:0}}.empty{{color:var(--muted)}}footer{{margin-top:38px;padding-top:18px;border-top:1px solid rgba(255,255,255,.08);color:var(--muted);font-size:10px;line-height:1.8}}@media(max-width:720px){{header{{padding:22px}}.market{{grid-template-columns:1fr 1fr}}.cards,.board-grid{{grid-template-columns:1fr}}}}</style></head><body><main><header><div class="eyebrow">DAILY INDUSTRY & TAG BRIEF</div><h1>{evidence['as_of']} 市场结构</h1><p>{html.escape(analysis['market_summary'])}</p><div class="market"><div><span>股票</span><b>{market['stock_count']}</b></div><div><span>上涨</span><b>{market['up_count']}</b></div><div><span>下跌</span><b>{market['down_count']}</b></div><div><span>涨跌中位</span><b>{pct(market.get('daily_return_median'), signed=True)}</b></div><div><span>强势分类</span><b>{ratio_pct(market.get('strong_state_ratio'))}</b></div></div></header><section><div class="section-title"><h2>市场整体总结</h2></div><div class="overall-summary"><p>{overall_summary}</p></div></section>{benchmark_section}{''.join(board_sections)}{''.join(sections)}<footer>{html.escape(evidence['scope_note'])}<br>报告模式：{html.escape(model_label)}。红黑榜由透明的多维条件生成；数字与排名由代码生成，大模型仅参与证据选择与文字归纳。仅供研究参考。</footer></main></body></html>"""
