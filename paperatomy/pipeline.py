"""完整流水线：解析 → 抽取 → 论证链 → 证据定位 → 渲染。

从 CLI 与工作台两个入口共用的流程里抽出来的。
放这一层的动机：入口（命令行 / 页面上传）关心的东西一样——
「给我一个 PDF，还我一组报告文件」，而中途每个模块（parse / extract /
chain / locate / render）不应该被入口的差异绑住。

进度通过 ``on_progress`` 回调给出，格式是给人看的短句；
工作台把它实时转给浏览器，命令行直接打印。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .llm import LLMConfig
from .locate import LocateReport, annotate_extraction
from .model import Template, list_templates
from .parse import Paper, parse
from .render import to_html, to_json, to_markdown

__all__ = ["AnalyzeResult", "analyze_pdf"]


class AnalyzeResult:
    """一次完整分析的产物。"""

    def __init__(self, *, json_path: Path, md_path: Path, html_path: Path,
                 paper: Paper, chain_nodes: int, locate: LocateReport):
        self.json_path = json_path
        self.md_path = md_path
        self.html_path = html_path
        self.paper = paper
        self.chain_nodes = chain_nodes
        self.locate = locate

    @property
    def title(self) -> str:
        import json
        data = json.loads(self.json_path.read_text(encoding="utf-8"))
        return str(data.get("paper_title") or self.json_path.stem)


def analyze_pdf(
    pdf: str | Path,
    template: Template,
    cfg: LLMConfig,
    *,
    lang: str = "zh",
    max_chars: int = 90_000,
    out_prefix: str | Path | None = None,
    repair_rounds: int = 2,
    skip_chain: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> AnalyzeResult:
    """跑完整流程，落盘 JSON / Markdown / HTML。

    参数
    ----
    out_prefix : 产物路径前缀（不含扩展名）。缺省为 PDF 同名。
    skip_chain : 跳过论证链（省一次模型调用）。

    异常
    ----
    模型调用失败、解析失败会直接抛出——由入口决定怎么呈现，
    这一层不做「失败也返回半成品」的决定。
    """
    say = on_progress or (lambda _m: None)
    pdf = Path(pdf)

    say(f"解析 {pdf.name} …")
    paper = parse(pdf)
    say(f"  {paper.total_pages} 页 · {paper.chars:,} 字符 · {len(paper.sections)} 个章节")

    say(f"抽取正文（模板 {template.id} · {lang}）…")
    from .extract import extract

    result = extract(
        paper, template, cfg,
        lang=lang, max_chars=max_chars, repair_rounds=repair_rounds,
        on_progress=say,
    )

    chain = None
    if not skip_chain:
        say("抽取论证链 …")
        from .chain import extract_chain

        try:
            chain = extract_chain(
                paper, cfg,
                lang=lang, max_chars=max_chars, repair_rounds=repair_rounds,
                on_progress=say,
            )
            say(f"  链条 {len(chain.nodes)} 个节点")
        except Exception as exc:  # 论证链是增强项，失败不拦报告
            say(f"  ⚠ 论证链抽取失败（不影响报告）：{exc}")

    say("证据定位（引文回原文匹配）…")
    from .locate import annotate_extraction

    try:
        loc = annotate_extraction(result, paper.paragraphs, chain)
        say(f"  {loc.summary()}")
    except Exception as exc:
        from .locate import LocateReport
        loc = LocateReport()
        say(f"  ⚠ 证据定位失败（不影响报告）：{exc}")

    prefix = Path(out_prefix) if out_prefix else pdf.with_suffix("")
    prefix.parent.mkdir(parents=True, exist_ok=True)

    say("渲染报告 …")
    json_path = prefix.with_suffix(".json")
    json_path.write_text(
        to_json(result, template, paper=paper, chain=chain), encoding="utf-8")
    md_path = prefix.with_suffix(".md")
    md_path.write_text(
        to_markdown(result, template, lang=lang, chain=chain), encoding="utf-8")
    html_path = prefix.with_suffix(".html")
    html_path.write_text(
        to_html(result, template, lang=lang, paper=paper, chain=chain),
        encoding="utf-8")

    return AnalyzeResult(
        json_path=json_path, md_path=md_path, html_path=html_path,
        paper=paper,
        chain_nodes=len(chain.nodes) if chain is not None else 0,
        locate=loc,
    )


def find_template(template_id: str) -> Template:
    """按 id 找模板，找不到抛 ValueError。"""
    for t in list_templates():
        if t.id == template_id:
            return t
    known = "、".join(t.id for t in list_templates())
    raise ValueError(f"没有这个模板：{template_id!r}（可用：{known}）")
