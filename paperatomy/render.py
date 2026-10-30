"""渲染：把抽取结果变成人能读的东西。

一期只做 Markdown 与 JSON。结构图在二期。

Markdown 的目标不是"好看"，是**能在终端里一路读下去**，
并且每块末尾把证据缩进列出 —— 让人边读边能核对。
"""

from __future__ import annotations

import json
from pathlib import Path

from .model import Extraction, Template

__all__ = ["to_markdown", "to_json", "to_html", "CONFIDENCE_MARK", "REPORT_TEMPLATE"]

#: 单文件报告模板。数据在构建时内嵌，产物零外部依赖。
REPORT_TEMPLATE = Path(__file__).resolve().parent / "report_template.html"

#: 置信度标记。低置信度必须显式提醒，否则用户会当成确定结论。
CONFIDENCE_MARK = {
    "high": "",
    "medium": "",
    "low": "  ⚠ 低置信度，建议核对原文",
}


def to_markdown(
    extraction: Extraction,
    template: Template,
    *,
    lang: str = "zh",
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
                loc = f"p.{ev.page}" if ev.page else "页码未标注"
                quote = ev.quote.replace("\n", " ").strip()
                if quote:
                    lines.append(f"- **{loc}** — {quote}")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def to_json(extraction: Extraction, template: Template, *, paper=None) -> str:
    """序列化为 JSON。

    元信息（页数、来源）一并存下来，这样 ``paperatomy render``
    能从 JSON 单独重建 HTML，不需要回头再读 PDF。
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
                "evidence": [
                    {"page": e.page, "quote": e.quote} for e in f.evidence
                ],
            }
            for f in extraction.findings
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def to_html(
    extraction: Extraction,
    template: Template,
    *,
    lang: str = "zh",
    paper=None,
    pages: int = 0,
) -> str:
    """渲染成单文件 HTML 报告。

    产物把数据内嵌进 ``<script type="application/json">``，
    不依赖任何外部资源——双击能打开，也能直接丢到静态托管上。

    参数
    ----
    paper : Paper, 可选
        传入后可以显示页数等元信息。
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
                "evidence": [] if empty else [
                    {"page": e.page, "quote": e.quote} for e in finding.evidence
                ],
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
    }

    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # 防止数据里出现 </script> 提前闭合脚本标签
    blob = blob.replace("</", "<\\/")

    html = REPORT_TEMPLATE.read_text(encoding="utf-8")
    return html.replace("/*__DATA__*/null", blob)
