"""渲染：把抽取结果变成人能读的东西。

三种产物，各有用途：

- **Markdown** —— 终端/编辑器里一路读下去，每块末尾把证据缩进列出
- **JSON** —— 机器可消费的完整数据（含论证链），也是重建其他产物的源
- **HTML** —— 单文件报告，数据内嵌、零外部依赖，可直接丢到静态托管

论证链在 Markdown 里只出「结构摘要」，真正的动画在 HTML 里——
动画要动，文本表达不了。
"""

from __future__ import annotations

import json
from pathlib import Path

from .chain import NODE_TYPES, Chain, chain_warnings
from .model import Extraction, Template

__all__ = [
    "to_markdown",
    "to_json",
    "to_html",
    "chain_from_json",
    "chain_payload",
    "CONFIDENCE_MARK",
    "REPORT_TEMPLATE",
]

#: 单文件报告模板。数据在构建时内嵌，产物零外部依赖。
REPORT_TEMPLATE = Path(__file__).resolve().parent / "report_template.html"

#: 置信度标记。低置信度必须显式提醒，否则用户会当成确定结论。
CONFIDENCE_MARK = {
    "high": "",
    "medium": "",
    "low": "  ⚠ 低置信度，建议核对原文",
}


# ---------------------------------------------------------------------------
# 论证链
# ---------------------------------------------------------------------------

def chain_payload(chain: Chain) -> dict:
    """序列化论证链，并把「不影响渲染但该让人知道」的警告一并带上。

    警告在生成时就算好，前端只负责显示——前端不做判断，只做呈现。
    """
    payload = chain.to_dict()
    payload["warnings"] = chain_warnings(chain)
    # 类型的中文名与配色下发给前端，避免前后端各写一份枚举
    payload["types"] = {
        name: {"label": spec.label, "color": spec.color}
        for name, spec in NODE_TYPES.items()
    }
    return payload


def chain_from_json(data: dict) -> Chain:
    """从 JSON 载荷还原论证链。"""
    raw = data.get("chain")
    if not isinstance(raw, dict):
        return Chain()
    return Chain.from_dict(raw)


def _diagram_text(spec: dict) -> str:
    """把图示规格压成一句人能读的话（Markdown 不画图）。"""
    kind = spec.get("kind", "")
    label = str(spec.get("label", "") or "").strip()
    if kind == "tokens":
        n = spec.get("n", "?")
        cadence = spec.get("cadence")
        pace = ""
        if isinstance(cadence, (int, float)):
            pace = "，节奏慢" if cadence >= 1.0 else "，节奏快"
        return f"{n} 个 token 逐个生成{pace}" + (f"（{label}）" if label else "")
    if kind == "blocks":
        items = "、".join(str(x) for x in (spec.get("items") or []))
        return f"模块：{items}" + (f"（{label}）" if label else "")
    if kind == "bars":
        parts = [
            f"{it.get('label', '')} {it.get('value', '')}"
            for it in (spec.get("items") or [])
            if isinstance(it, dict)
        ]
        return "对比：" + " / ".join(parts)
    return str(spec.get("text", "")).strip() or label or "（无图示）"


def _taxonomy_lines(node, depth: int) -> list[str]:
    if not isinstance(node, dict):
        return []
    pad = "  " * depth
    what = str(node.get("what", "") or "").strip()
    line = f"{pad}- {node.get('name', '')}" + (f"：{what}" if what else "")
    out = [line]
    for child in node.get("children") or []:
        out.extend(_taxonomy_lines(child, depth + 1))
    return out


def _chain_lines(chain: Chain) -> list[str]:
    """把论证链渲染成 Markdown：一条带编号的推理线。"""
    out: list[str] = []
    if chain.thesis:
        out.append(f"> **主张**：{chain.thesis}")
        out.append("")

    for i, node in enumerate(chain.nodes, 1):
        out.append(f"{i}. **{node.label}**　`{node.type_label}`")
        if node.detail:
            out.append(f"   {node.detail}")

        if node.type == "change" and node.before and node.after:
            out.append(f"   - 改动前：{_diagram_text(node.before)}")
            out.append(f"   - 改动后：{_diagram_text(node.after)}")
        if node.type == "effect" and node.metric:
            value = node.metric.get("value", "")
            unit = node.metric.get("unit", "")
            label = node.metric.get("label", "")
            out.append(f"   - 数字：**{value}{unit}**（{label}）")
        if node.type == "taxonomy" and node.root:
            out.extend("   " + line for line in _taxonomy_lines(node.root, 0))

        for ev in node.evidence:
            mark = "　⚠ 未在原文定位" if ev.verified is False else ""
            out.append(f"   - _{ev.locator}_{mark}：{ev.quote}")
            if ev.quote_zh and ev.quote_zh != ev.quote:
                out.append(f"     {ev.quote_zh}")
        out.append("")
    return out


# ---------------------------------------------------------------------------
# 三种产物
# ---------------------------------------------------------------------------

def to_markdown(
    extraction: Extraction,
    template: Template,
    *,
    lang: str = "zh",
    chain: Chain | None = None,
    network: dict | None = None,
) -> str:
    """把结果渲染成 Markdown。"""
    lines: list[str] = []
    title = extraction.paper_title or "（未识别标题）"
    lines.append(f"# {title}")
    lines.append("")

    meta = [f"模板：{template.name}（{template.id}）", f"语言：{lang}"]
    if extraction.model:
        meta.append(f"模型：{extraction.model}")
    lines.append("  ·  ".join(meta))
    lines.append("")

    if chain is not None and chain.nodes:
        lines.append("## 论证链")
        lines.append("")
        lines.append("> 在 HTML 报告里可以逐段播放；这里只列结构。")
        lines.append("")
        lines.extend(_chain_lines(chain))

    if network and network.get("nodes"):
        lines.append("## 文献网络")
        lines.append("")
        st = network.get("stats") or {}
        lines.append(f"> {st.get('reference_entries', '?')} 条参考文献中 "
                     f"{st.get('matched', '?')} 条在 OpenAlex 命中并补全了 DOI 与被引数；"
                     "节点全部来自真实数据，非模型生成。")
        lines.append("")
        for n in network["nodes"][:12]:
            doi = f" [DOI](https://doi.org/{n['doi']})" if n.get("doi") else ""
            lines.append(f"- **{n['title']}**（{n.get('year') or '?'}，"
                         f"被引 {n['cited_by']}，相关度 {int(n['relevance']*100)}%）{doi}")
        lines.append("")

    by_block = extraction.by_block()
    current_group = ""

    for block in template.blocks:
        finding = by_block.get(block.id)

        group = block.group_in(lang)
        if group and group != current_group:
            current_group = group
            lines.append(f"## {group}")
            lines.append("")

        heading = block.title_in(lang)
        if finding is None or finding.is_empty:
            lines.append(f"### {heading}")
            lines.append("")
            lines.append("_论文未提及或材料不足，未抽取到内容。_")
            lines.append("")
            continue

        lines.append(f"### {heading}")
        if finding.confidence == "low":
            lines.append("")
            lines.append("> ⚠ 低置信度：模型主要是推断，建议核对原文。")
        lines.append("")
        lines.append(finding.content.strip())
        lines.append("")

        if finding.evidence:
            lines.append("<details><summary>原文依据</summary>")
            lines.append("")
            for ev in finding.evidence:
                quote = ev.quote.replace("\n", " ").strip()
                if not quote:
                    continue
                mark = "　⚠ 未在原文定位到原话" if ev.verified is False else ""
                lines.append(f"- **{ev.locator}**{mark}")
                lines.append(f"  - 原文：{quote}")
                if ev.quote_zh and ev.quote_zh != ev.quote:
                    zh = ev.quote_zh.replace("\n", " ").strip()
                    lines.append(f"  - 译文：{zh}")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def to_json(
    extraction: Extraction,
    template: Template,
    *,
    paper=None,
    chain: Chain | None = None,
    network: dict | None = None,
) -> str:
    """序列化为 JSON。

    元信息（页数、来源）与论证链一并存下来，这样 ``paperatomy render``
    能从 JSON 单独重建 HTML，不需要回头再读 PDF、也不用重新调模型。
    """
    payload = {
        "template": {
            "id": template.id,
            "version": template.version,
            "name": template.name,
        },
        "paper_title": extraction.paper_title,
        "source": getattr(paper, "path", "") if paper is not None else "",
        "pages": getattr(paper, "total_pages", 0) if paper is not None else 0,
        "lang": extraction.lang,
        "model": extraction.model,
        "findings": [
            {
                "block_id": f.block_id,
                "content": f.content,
                "confidence": f.confidence,
                "evidence": [e.to_dict() for e in f.evidence],
            }
            for f in extraction.findings
        ],
        "chain": chain_payload(chain) if chain is not None and chain.nodes else None,
        "network": network,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def to_html(
    extraction: Extraction,
    template: Template,
    *,
    lang: str = "zh",
    paper=None,
    pages: int = 0,
    chain: Chain | None = None,
    network: dict | None = None,
) -> str:
    """渲染成单文件 HTML 报告。

    产物把数据与论证链内嵌进 ``<script type="application/json">``，
    不依赖任何外部资源——双击能打开，也能直接丢到静态托管上。

    参数
    ----
    paper : Paper, 可选
        传入后可以显示页数等元信息。
    chain : Chain, 可选
        论证链。为空时报告里不出现论证链章节。
    """
    by_block = extraction.by_block()
    groups: list[dict] = []

    for group_zh, group_en in template.groups():
        name = group_en if (lang.startswith("en") and group_en) else (group_zh or "未分组")
        blocks: list[dict] = []
        for block in template.blocks:
            if (block.group, block.group_en) != (group_zh, group_en):
                continue
            finding = by_block.get(block.id)
            empty = finding is None or finding.is_empty
            blocks.append({
                "id": block.id,
                "title": block.title_in(lang),
                "content": "" if empty else finding.content,
                "confidence": "medium" if empty else finding.confidence,
                "empty": empty,
                "evidence": [] if empty else [e.to_dict() for e in finding.evidence],
            })
        if blocks:
            groups.append({"name": name or "未分组", "blocks": blocks})

    payload = {
        "paper_title": extraction.paper_title,
        "template_name": template.name,
        "model": extraction.model,
        "lang": lang,
        "pages": pages or (getattr(paper, "total_pages", 0) if paper is not None else 0),
        "blocks": sum(len(g["blocks"]) for g in groups),
        "groups": groups,
        "chain": chain_payload(chain) if chain is not None and chain.nodes else None,
        "network": network,
    }

    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # 防止数据里出现 </script> 提前闭合脚本标签
    blob = blob.replace("</", "<\\/")

    html = REPORT_TEMPLATE.read_text(encoding="utf-8")
    return html.replace("/*__DATA__*/null", blob)
