"""综述导出：把多篇报告合成一份文献综述草稿（Markdown）。

零模型调用——每篇报告的数据已经在 JSON 里，这里按固定的综述骨架
把它们拼起来：一篇一节，节内是速读、逐板块要点（带证据定位）、
论证链一行摘要；末尾汇总检索关键词，供下一步找文献用。

诚实的定位：这是**草稿**——把分散的报告变成一份能连续阅读的材料，
不是替你写好的综述。观点的整合与批判仍是读者的事。
"""
from __future__ import annotations

import json
from pathlib import Path

__all__ = ["review_markdown", "run_review"]


def _load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "findings" not in data:
        raise ValueError(f"{path.name} 不是 paperatomy 的报告 JSON")
    return data


def _tpl_name(t) -> str:
    if isinstance(t, dict):
        return str(t.get("id") or t.get("name") or "")
    return str(t or "")


def _chain_line(nodes: list) -> str:
    if not nodes:
        return "（无可演的链）"
    return " → ".join(
        f"{n.get('type', '?')}·{str(n.get('label') or '')[:20]}" for n in nodes)


def _center_info(d: dict) -> list[str]:
    net = d.get("network") or {}
    c = net.get("center") or {}
    out = []
    if c.get("venue"):
        venue = c["venue"]
        if c.get("ccf"):
            venue += f"（CCF-{c['ccf']}）"
        out.append(f"- **发表场所**：{venue}")
    topics = c.get("topics") or []
    if topics:
        parts = []
        for tp in topics[:2]:
            bits = [tp.get("field_zh") or tp.get("field"),
                    tp.get("subfield_zh") or tp.get("subfield")]
            parts.append(" · ".join(x for x in bits if x))
        if parts:
            out.append(f"- **研究领域**：{'；'.join(parts)}")
    terms = c.get("search_terms") or []
    if terms:
        out.append("- **检索关键词**：" + "、".join(f"`{t}`" for t in terms))
    return out


def _paper_section(idx: int, d: dict) -> str:
    out = [f"## {idx}. {d.get('paper_title') or '(未识别标题)'}", ""]
    info = _center_info(d)
    if info:
        out += info
    if not info:
        out.append("- *本文未在 OpenAlex 收录，场所与领域信息缺省*")
    out.append("")

    findings = d.get("findings") or []
    tl = next((f for f in findings if f.get("block_id") == "tl_dr"), None)
    if tl:
        out += [f"> **一句话**：{tl.get('content', '')}", ""]

    for f in findings:
        if f.get("block_id") == "tl_dr":
            continue
        out.append(f"### {f.get('label') or f.get('block_id')}")
        out.append("")
        out.append(str(f.get("content") or "").strip())
        evs = f.get("evidence") or []
        if evs:
            out.append("")
            for e in evs[:2]:
                loc = "§%s·第%s段·p.%s" % (
                    e.get("section_no") or "?", e.get("para") or "?",
                    e.get("page") or "?")
                mark = "" if e.get("verified") else "（未核实）"
                zh = e.get("quote_zh") or ""
                out.append(f"> {loc}{mark} —— {zh or e.get('quote', '')}")
        out.append("")

    chain = (d.get("chain") or {}).get("nodes") or []
    out += ["**论证链**：" + _chain_line(chain), ""]
    return "\n".join(out)


def review_markdown(reports: list[dict], names: list[str]) -> str:
    lines = ["# 文献综述草稿", "",
             f"> 由 {len(reports)} 份 paperatomy 报告合成（"
             + "、".join(names) + "）。"
             "证据定位与「未核实」标记的含义与各报告一致；"
             "本文件是程序合成的阅读底稿，不是成稿。", ""]

    # 关键词汇总：按出现次数排序，给「下一步往哪搜」
    seen: dict[str, int] = {}
    for d in reports:
        for t in ((d.get("network") or {}).get("center") or {}).get(
                "search_terms") or []:
            k = t.lower()
            seen[k] = seen.get(k, 0) + 1
    if seen:
        ranked = sorted(seen.items(), key=lambda x: -x[1])[:12]
        lines += ["## 检索关键词汇总", "",
                  "多篇报告共现的关键词排前面——它们大概率是这一批论文的公共主题：", ""]
        lines += ["- " + "、".join(f"**{t}**（{n} 篇）" if n > 1 else f"{t}"
                                   for t, n in ranked), ""]

    for i, d in enumerate(reports, 1):
        lines.append(_paper_section(i, d))
        lines.append("---")
        lines.append("")

    lines.append("*合成自 paperatomy 报告；观点整合与批判性阅读仍需人工完成。*")
    return "\n".join(lines)


def run_review(paths: list[str], output: str | None = None) -> int:
    reports, names = [], []
    for p in paths:
        path = Path(p)
        if not path.is_file():
            print(f"✗ 找不到文件：{path}")
            return 1
        reports.append(_load(path))
        names.append(path.stem)

    if len(reports) < 2:
        print("✗ 综述合成至少需要两份报告")
        return 1

    md = review_markdown(reports, names)
    out = (Path(output) if output
           else Path(paths[0]).parent / "文献综述草稿.md")
    out.write_text(md, encoding="utf-8")
    print(f"已写出 {out}  （{len(reports)} 篇合成，零模型调用）")
    return 0
