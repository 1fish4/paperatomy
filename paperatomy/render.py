"""渲染：把抽取结果变成人能读的东西。

三种产物，各有用途：

- **Markdown** —— 终端/编辑器里一路读下去，每块末尾把证据缩进列出
- **JSON** —— 机器可消费的完整数据（含可视化脚本），也是重建其他产物的源
- **HTML** —— 单文件报告，数据内嵌、零外部依赖，可直接丢到静态托管

可视化章节只做「脚本摘要」：原文里列清楚这段演示演什么、分几步。
真正的动画在前端，因为动画要动，文本表达不了。
"""

from __future__ import annotations

import json
from pathlib import Path

from .model import Extraction, Template

__all__ = [
    "to_markdown",
    "to_json",
    "to_html",
    "visuals_from_json",
    "visual_payload",
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


def _visual_lines(visual, index: int) -> list[str]:
    """把一个可视化脚本渲染成可读的文字摘要。

    只列结构，不描述动画——动画在 HTML 里。
    """
    from .visuals import PRIMITIVES

    spec = PRIMITIVES.get(visual.primitive)
    label = spec.label if spec else visual.primitive
    out = [f"### {index}. {visual.title or '（未命名演示）'}", ""]
    out.append(f"_原语：{label}（`{visual.primitive}`）_")
    out.append("")

    params = visual.params or {}
    if visual.primitive == "pipeline":
        for i, st in enumerate(params.get("stages") or [], 1):
            name = str(st.get("name", "")).strip()
            what = str(st.get("what", "")).strip()
            why = str(st.get("why", "")).strip()
            out.append(f"{i}. **{name}** —— {what}")
            out.append(f"   - 为什么必须有：{why or '（原文未说明）'}")
    elif visual.primitive == "example":
        out.append(f"**设定**：{params.get('scenario', '')}")
        out.append("")
        for i, st in enumerate(params.get("steps") or [], 1):
            state = str(st.get("state", "")).strip()
            issue = str(st.get("issue") or "").strip()
            if issue:
                out.append(f"{i}. {state}  ⟵ **卡点：{issue}**")
            else:
                out.append(f"{i}. {state}")
    elif visual.primitive == "taxonomy":
        def walk(node, depth: int) -> None:
            if not isinstance(node, dict):
                return
            pad = "  " * depth
            what = str(node.get("what", "")).strip()
            out.append(f"{pad}- **{node.get('name', '')}**"
                       + (f" —— {what}" if what else ""))
            for child in node.get("children") or []:
                walk(child, depth + 1)

        walk(params.get("root"), 0)

    out.append("")
    if visual.evidence:
        out.append("<details><summary>原文依据</summary>")
        out.append("")
        for ev in visual.evidence:
            loc = f"p.{ev.page}" if ev.page else "页码未标注"
            out.append(f"- **{loc}** — {ev.quote}")
        out.append("")
        out.append("</details>")
        out.append("")
    return out


def to_markdown(
    extraction: Extraction,
    template: Template,
    *,
    lang: str = "zh",
    visuals: list | None = None,
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

    if visuals:
        lines.append("## 动态演示")
        lines.append("")
        lines.append("> 在 HTML 报告里可播放；这里只列出脚本结构。")
        lines.append("")
        for i, v in enumerate(visuals, 1):
            lines.extend(_visual_lines(v, i))

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


def visual_payload(visual) -> dict:
    """序列化一个可视化脚本，并把「不影响渲染但该让人知道」的警告一并带上。

    最典型的是 pipeline 某阶段没有 ``why``——说明论文没讲清这一步的必要性。
    这条信息在生成时就算好，前端只负责显示。
    """
    from .visuals import visual_warnings

    payload = visual.to_dict()
    payload["warnings"] = visual_warnings(visual)
    return payload


def to_json(
    extraction: Extraction,
    template: Template,
    *,
    paper=None,
    visuals: list | None = None,
) -> str:
    """序列化为 JSON。

    元信息（页数、来源）与可视化脚本一并存下来，这样 ``paperatomy render``
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
                "evidence": [
                    {"page": e.page, "quote": e.quote} for e in f.evidence
                ],
            }
            for f in extraction.findings
        ],
        "visuals": [visual_payload(v) for v in (visuals or [])],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def visuals_from_json(data: dict) -> list:
    """从 JSON 载荷还原可视化脚本列表。"""
    from .visuals import Visual

    return [
        Visual.from_dict(item)
        for item in (data.get("visuals") or [])
        if isinstance(item, dict)
    ]


def to_html(
    extraction: Extraction,
    template: Template,
    *,
    lang: str = "zh",
    paper=None,
    pages: int = 0,
    visuals: list | None = None,
) -> str:
    """渲染成单文件 HTML 报告。

    产物把数据与可视化脚本内嵌进 ``<script type="application/json">``，
    不依赖任何外部资源——双击能打开，也能直接丢到静态托管上。

    参数
    ----
    paper : Paper, 可选
        传入后可以显示页数等元信息。
    visuals : list[Visual], 可选
        可视化脚本。为空时报告里不出现动态演示章节。
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
        "visuals": [visual_payload(v) for v in (visuals or [])],
    }

    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # 防止数据里出现 </script> 提前闭合脚本标签
    blob = blob.replace("</", "<\\/")

    html = REPORT_TEMPLATE.read_text(encoding="utf-8")
    return html.replace("/*__DATA__*/null", blob)
