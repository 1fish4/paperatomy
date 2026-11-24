"""Shared method reading dimensions for the lab and model comparison."""

import re

DIMENSIONS = (
    ("研究目标", ("problem", "question", "gap")),
    ("输入、输出与约束", ("method_overview", "method", "setup")),
    ("核心决策与执行步骤", ("method_steps", "mechanism", "method_details")),
    ("数据、基线与评价", ("dataset", "setup", "result", "consensus")),
    ("适用范围与局限", ("limits", "limitations")),
)


def method_profile(data: dict) -> list[dict]:
    findings = {block.get("block_id"): block for block in (data.get("findings") or [])
                if isinstance(block, dict)}
    profile = []
    for label, keys in DIMENSIONS:
        parts = []
        pages = set()
        for key in keys:
            item = findings.get(key) or {}
            content = str(item.get("content") or "").strip()
            if content:
                parts.append(content)
            for evidence in item.get("evidence") or []:
                if not isinstance(evidence, dict) or evidence.get("verified") is not True:
                    continue
                page = evidence.get("located_page") or evidence.get("page")
                if type(page) is int and 0 < page < 1000:
                    pages.add(page)
        profile.append({"label": label, "text": "\n\n".join(parts), "pages": sorted(pages)})
    return profile


def method_digest(data: dict) -> str:
    lines = [f"论文：{data.get('paper_title')}",
             f"模板：{(data.get('template') or {}).get('id', '')}"]
    for section in method_profile(data):
        text = section["text"][:1800] or "当前报告未抽取"
        lines.append(f"{section['label']}：{text}")
    for node in ((data.get("chain") or {}).get("nodes") or [])[:12]:
        if not isinstance(node, dict):
            continue
        label = node.get("label") or node.get("title") or ""
        detail = node.get("detail") or node.get("description") or ""
        if label:
            lines.append(f"论证链 · {label} {detail}"[:300])
    return "\n".join(lines)


def asap_source(pdf) -> int | None:
    """Locate an explicit fixed-mapping ASAP rule before linking the teaching case."""
    if pdf is None:
        return None
    import pymupdf

    try:
        with pymupdf.open(pdf) as document:
            for page in document:
                text = re.sub(r"(?<=\w)-\s+(?=\w)", "", page.get_text())
                text = " ".join(text.lower().split())
                if re.search(r"asap baseline algorithm starts? each task.{0,50}"
                             r"earliest possible start time", text):
                    return page.number + 1
    except (OSError, RuntimeError, ValueError):
        return None
    return None
