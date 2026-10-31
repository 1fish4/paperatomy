"""命令行入口。

设计原则：**每一步都能单独跑**。

``parse`` 只解析不调模型，``extract`` 才花钱。
调试解析问题时不用浪费 token，这是分开的主要原因。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

__all__ = ["main", "build_parser"]


def enable_utf8() -> None:
    """英文 Windows 的默认输出编码是 cp1252，打印中文会抛异常。

    必须在入口处调用，不能在模块导入时执行（导入即产生副作用是不该有的行为）。
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paperatomy",
        description="把一篇论文解剖成结构化的骨架：问题、方法、数据、结论，每块带原文出处。",
    )
    sub = parser.add_subparsers(dest="command")

    p_tpl = sub.add_parser("templates", help="列出可用的模板")
    p_tpl.add_argument("--dir", default=None, help="自定义模板目录")

    p_parse = sub.add_parser("parse", help="只解析 PDF，不调用大模型（调试用）")
    p_parse.add_argument("pdf", help="PDF 文件路径")
    p_parse.add_argument("--preview", type=int, default=600,
                         help="预览前多少个字符（默认 600）")

    p_ext = sub.add_parser("extract", help="按模板抽取论文内容")
    p_ext.add_argument("pdf", help="PDF 文件路径")
    p_ext.add_argument("-t", "--template", default="skim",
                       help="模板 id（默认 skim）")
    p_ext.add_argument("-l", "--lang", default="zh", choices=["zh", "en", "zh+en"],
                       help="输出语言（默认 zh）")
    p_ext.add_argument("-o", "--output", default=None,
                       help="输出文件前缀；默认与 PDF 同名同目录")
    p_ext.add_argument("--max-chars", type=int, default=90_000,
                       help="送给模型的材料上限（默认 90000）")
    p_ext.add_argument("--repair-rounds", type=int, default=2,
                       help="校验失败后的修复轮数（默认 2）")
    p_ext.add_argument("--json-only", action="store_true",
                       help="只写 JSON，不写 Markdown 与 HTML")
    p_ext.add_argument("--no-html", action="store_true",
                       help="不生成 HTML 报告")
    p_ext.add_argument("--no-chain", action="store_true",
                       help="跳过论证链抽取（省一次模型调用）")

    p_node = sub.add_parser("nodes", help="列出论证链的节点类型")
    p_node.add_argument("--json", action="store_true", help="以 JSON 输出")

    p_ren = sub.add_parser("render", help="从已有的 JSON 重新生成 Markdown 与 HTML（不调用模型）")
    p_ren.add_argument("json", help="extract 产出的 JSON 文件")
    p_ren.add_argument("-l", "--lang", default=None,
                       help="输出语言，默认沿用 JSON 里记录的")
    p_ren.add_argument("-o", "--output", default=None,
                       help="输出文件前缀，默认与 JSON 同名")

    p_chain = sub.add_parser(
        "chain", help="为已有结果抽取论证链（只调一次模型，不动正文）")
    p_chain.add_argument("json", help="extract 产出的 JSON 文件")
    p_chain.add_argument("--max-chars", type=int, default=90_000,
                         help="送给模型的材料上限（默认 90000）")
    p_chain.add_argument("--rebuild", action="store_true", default=True,
                         help="补完后自动重建 Markdown 与 HTML（默认开启）")
    p_chain.add_argument("--no-rebuild", dest="rebuild", action="store_false")

    p_ext.add_argument("--api-key", default=None)
    p_ext.add_argument("--base-url", default=None)
    p_ext.add_argument("--model", default=None)

    p_cfg = sub.add_parser("config", help="查看或设置大模型配置")
    p_cfg.add_argument("action", nargs="?", default="show",
                       choices=["show", "set"])
    p_cfg.add_argument("--api-key", default=None)
    p_cfg.add_argument("--base-url", default=None)
    p_cfg.add_argument("--model", default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    enable_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "templates":
        return _cmd_templates(args)
    if args.command == "nodes":
        return _cmd_nodes(args)
    if args.command == "parse":
        return _cmd_parse(args)
    if args.command == "extract":
        return _cmd_extract(args)
    if args.command == "render":
        return _cmd_render(args)
    if args.command == "chain":
        return _cmd_chain(args)
    if args.command == "config":
        return _cmd_config(args)

    parser.print_help()
    return 1


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def _cmd_templates(args) -> int:
    from .model import list_templates

    try:
        templates = list_templates(args.dir)
    except Exception as exc:
        print(f"✗ 加载模板失败：{exc}")
        return 1

    if not templates:
        print("没有找到任何模板。")
        return 1

    print(f"共 {len(templates)} 套模板：\n")
    for t in templates:
        required = len(t.required_ids)
        print(f"  {t.id:<10} {t.name:<8} {len(t.blocks)} 块（必填 {required}）"
              f"  ~{t.estimated_minutes} 分钟")
        if t.description:
            print(f"             {t.description}")
    print()
    print("用法：paperatomy extract paper.pdf -t <模板 id>")
    return 0


def _cmd_nodes(args) -> int:
    import json as _json

    from .chain import list_node_types

    specs = list_node_types()
    if args.json:
        print(_json.dumps(
            [{"name": s.name, "label": s.label, "guidance": s.guidance} for s in specs],
            ensure_ascii=False, indent=2,
        ))
        return 0

    print(f"论证链共 {len(specs)} 种节点类型：\n")
    for s in specs:
        need = ""
        if s.needs_before_after:
            need = "  [必须带前后对照图示]"
        elif s.needs_metric:
            need = "  [必须带数字]"
        print(f"  {s.name:<10} {s.label}{need}")
        print(f"             {s.guidance}")
        print()
    print("用法：paperatomy extract paper.pdf -t deep   （会自动抽取论证链）")
    print("      一篇论文的链条长度与顺序由它自己决定，不是固定模板。")
    return 0


def _cmd_parse(args) -> int:
    from .parse import parse

    try:
        paper = parse(args.pdf)
    except Exception as exc:
        print(f"✗ 解析失败：{exc}")
        return 1

    print(f"标题：{paper.title or '（未识别）'}")
    print(f"页数：{paper.total_pages}    字符数：{paper.chars:,}")
    print(f"章节：{len(paper.sections)} 个")
    print()
    print("章节划分：")
    skipped = 0
    for sec in paper.sections:
        tag = ""
        if sec.is_reference:
            tag = "   ← 参考文献，抽取时跳过"
            skipped += sec.chars
        print(f"  p.{sec.start_page:<3} {sec.title[:50]:<52} "
              f"{sec.chars:>8,} 字符{tag}")
    if skipped:
        print(f"\n  参考文献共 {skipped:,} 字符，占全篇 "
              f"{skipped / max(paper.chars, 1) * 100:.0f}%，不参与抽取。")

    if args.preview > 0:
        print()
        print(f"首页预览（前 {args.preview} 字符）：")
        print("-" * 60)
        head = paper.pages[0].text[: args.preview] if paper.pages else ""
        print(head or "（首页没有可用文本，可能是扫描版 PDF）")
        print("-" * 60)
    return 0


def _cmd_extract(args) -> int:
    from .extract import extract
    from .llm import load_config
    from .model import list_templates
    from .parse import parse
    from .render import to_html, to_json, to_markdown

    # 模板
    try:
        templates = {t.id: t for t in list_templates()}
    except Exception as exc:
        print(f"✗ 加载模板失败：{exc}")
        return 1
    template = templates.get(args.template)
    if template is None:
        print(f"✗ 没有这个模板：{args.template}")
        print(f"  可用：{'、'.join(templates) or '（无）'}")
        return 1

    # 配置
    cfg = load_config(
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
    )
    if not cfg.api_key and not cfg.is_local():
        print("✗ 没有配置 API key。任选一种：")
        print("   1. 设置环境变量 PAPERATOMY_API_KEY")
        print("   2. paperatomy config set --api-key sk-xxx")
        print("   3. 命令行加 --api-key sk-xxx")
        return 1

    # 解析
    print(f"读取 {args.pdf} …")
    try:
        paper = parse(args.pdf)
    except Exception as exc:
        print(f"✗ 解析失败：{exc}")
        return 1
    print(f"  {paper.total_pages} 页 · {paper.chars:,} 字符 · {len(paper.sections)} 个章节")

    # 抽取
    try:
        result = extract(
            paper, template, cfg,
            lang=args.lang,
            max_chars=args.max_chars,
            repair_rounds=args.repair_rounds,
            on_progress=lambda msg: print("  " + msg),
        )
    except Exception as exc:
        print(f"✗ 抽取失败：{exc}")
        return 1

    # 论证链。失败不影响主结果——报告本身仍然可用。
    chain = None
    if not args.no_chain:
        from .chain import extract_chain

        try:
            chain = extract_chain(
                paper, cfg,
                lang=args.lang,
                max_chars=args.max_chars,
                on_progress=lambda msg: print("  " + msg),
            )
        except Exception as exc:
            print(f"  ⚠ 论证链抽取失败（不影响报告）：{exc}")

    # 落盘
    pdf_path = Path(args.pdf)
    prefix = Path(args.output) if args.output else pdf_path.with_suffix("")
    prefix.parent.mkdir(parents=True, exist_ok=True)

    json_path = prefix.with_suffix(".json")
    json_path.write_text(
        to_json(result, template, paper=paper, chain=chain), encoding="utf-8"
    )
    print(f"\n已写出 {json_path}")

    if not args.json_only:
        md_path = prefix.with_suffix(".md")
        md_path.write_text(
            to_markdown(result, template, lang=args.lang, chain=chain),
            encoding="utf-8",
        )
        print(f"已写出 {md_path}")

        if not args.no_html:
            html_path = prefix.with_suffix(".html")
            html_path.write_text(
                to_html(result, template, lang=args.lang, paper=paper,
                        chain=chain),
                encoding="utf-8",
            )
            print(f"已写出 {html_path}  ← 用浏览器打开这个")

    # 终端摘要
    print()
    print("=" * 62)
    by_block = result.by_block()
    for block in template.blocks:
        finding = by_block.get(block.id)
        if finding is None or finding.is_empty:
            print(f"[ ] {block.title}  —— 未抽取到")
            continue
        flag = "!" if finding.confidence == "low" else " "
        head = finding.content.replace("\n", " ")[:70]
        print(f"[{flag}] {block.title}：{head}")
    print("=" * 62)
    return 0


def _cmd_render(args) -> int:
    """从 JSON 重新渲染，不调用模型。

    改样式、改模板标题的时候用这个：
    同一份抽取结果可以反复出不同产物，不重复花钱。
    """
    import json as _json

    json_path = Path(args.json)
    if not json_path.is_file():
        print(f"✗ 找不到文件：{json_path}")
        return 1

    try:
        data = _json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"✗ JSON 解析失败：{exc}")
        return 1

    return _render_from_data(json_path, data, lang=args.lang, output=args.output)


def _cmd_chain(args) -> int:
    """给已有结果补论证链。

    只调用一次模型，**不动正文**——正文往往已经人工核对过，
    为了加一条动画链重跑整篇抽取会把它改掉，那是倒过来的取舍。
    """
    import json as _json

    from .chain import extract_chain
    from .llm import load_config
    from .parse import parse
    from .render import chain_payload

    json_path = Path(args.json)
    if not json_path.is_file():
        print(f"✗ 找不到文件：{json_path}")
        return 1
    try:
        data = _json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"✗ JSON 解析失败：{exc}")
        return 1

    source = str(data.get("source", "") or "")
    if not source:
        print("✗ JSON 里没有记录论文来源路径，无法重新读取原文。")
        print("  请改用 paperatomy extract 重新生成。")
        return 1
    pdf_path = _resolve_source(source, json_path)
    if pdf_path is None:
        print(f"✗ 找不到原文：{source}")
        print("  在 JSON 所在目录或当前目录下都没有找到，"
              "请把 PDF 放回原位，或重新跑 paperatomy extract。")
        return 1

    cfg = load_config()
    if not cfg.api_key and not cfg.is_local():
        print("✗ 没有配置 API key。先跑 paperatomy config set --api-key sk-xxx")
        return 1

    print(f"读取 {pdf_path.name} …")
    try:
        paper = parse(pdf_path)
    except Exception as exc:
        print(f"✗ 解析失败：{exc}")
        return 1

    try:
        chain = extract_chain(
            paper, cfg,
            lang=str(data.get("lang", "zh")),
            max_chars=args.max_chars,
            on_progress=lambda msg: print("  " + msg),
        )
    except Exception as exc:
        print(f"✗ 论证链抽取失败：{exc}")
        return 1

    data["chain"] = chain_payload(chain) if chain.nodes else None
    # 0.2 时代的 visuals 字段已废弃，顺手清掉，避免 JSON 里留两套并存的结构
    data.pop("visuals", None)
    json_path.write_text(
        _json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已更新 {json_path}（{len(chain.nodes)} 个节点）")

    if args.rebuild:
        return _render_from_data(json_path, data)
    return 0


def _resolve_source(source: str, json_path: Path) -> Path | None:
    """把 JSON 里记的来源路径还原成磁盘上真实存在的文件。

    ``source`` 是抽取时按当时的工作目录记录的相对路径，
    可能相对当前目录、也可能相对 JSON 所在目录（输出到子目录时）。
    两个都试，取第一个存在的——比强行规定一种语义更不容易出错。
    """
    raw = Path(source)
    candidates = [raw] if raw.is_absolute() else [
        raw,
        json_path.parent / raw,
        json_path.parent.parent / raw,
    ]
    for cand in candidates:
        if cand.is_file():
            return cand.resolve()
    return None


def _render_from_data(json_path: Path, data: dict, *, lang=None, output=None) -> int:
    """由 JSON 载荷重建 Markdown 与 HTML。``render`` 与 ``chain`` 共用。"""
    from .model import Evidence, Extraction, Finding, list_templates
    from .render import chain_from_json, to_html, to_markdown

    template_id = (data.get("template") or {}).get("id", "")
    templates = {t.id: t for t in list_templates()}
    template = templates.get(template_id)
    if template is None:
        print(f"✗ 找不到模板：{template_id!r}（可用：{'、'.join(templates)}）")
        return 1

    findings = []
    for item in data.get("findings") or []:
        findings.append(Finding(
            block_id=str(item.get("block_id", "")),
            content=str(item.get("content", "")),
            confidence=str(item.get("confidence", "medium")),
            evidence=[
                Evidence(page=int(e.get("page", 0) or 0), quote=str(e.get("quote", "")))
                for e in (item.get("evidence") or [])
            ],
        ))

    extraction = Extraction(
        template_id=template_id,
        paper_title=str(data.get("paper_title", "")),
        lang=lang or str(data.get("lang", "zh")),
        model=str(data.get("model", "")),
        findings=findings,
    )

    prefix = Path(output) if output else json_path.with_suffix("")
    chain = chain_from_json(data)

    md_path = prefix.with_suffix(".md")
    md_path.write_text(
        to_markdown(extraction, template, lang=extraction.lang, chain=chain),
        encoding="utf-8",
    )
    print(f"已写出 {md_path}")

    pages = int(data.get("pages", 0) or 0)
    html_path = prefix.with_suffix(".html")
    html_path.write_text(
        to_html(extraction, template, lang=extraction.lang,
                pages=pages, chain=chain), encoding="utf-8"
    )
    print(f"已写出 {html_path}")
    if chain.nodes:
        print(f"  含论证链 {len(chain.nodes)} 个节点")
    return 0


def _cmd_config(args) -> int:
    from .llm import CONFIG_PATH, load_config, save_config

    if args.action == "show":
        cfg = load_config()
        masked = "（未设置）"
        if cfg.api_key:
            masked = cfg.api_key[:6] + "…" + cfg.api_key[-4:] if len(cfg.api_key) > 12 \
                else "已设置"
        print(f"配置文件：{CONFIG_PATH}")
        print(f"  api_key  : {masked}")
        print(f"  base_url : {cfg.base_url}")
        print(f"  model    : {cfg.model}")
        return 0

    cfg = load_config(
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
    )
    path = save_config(cfg)
    print(f"✓ 已保存到 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
