"""多篇对比：把同一模板下的两份报告并排成一张对照页。

零模型调用——两份 JSON 的数据都在，这里只做对齐与渲染。
对比的意义在「差异」：同一板块两篇各说了什么、论证链各走哪条线，
所以页面以板块为行、两篇为列，差异处高亮。

不变式与报告一致：只呈现已有数据，不生成任何新判断。
"""
from __future__ import annotations

import html
import json
from pathlib import Path

__all__ = ["compare_payload", "to_compare_html",
            "compare_many_payload", "to_compare_html_many"]


def _load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "findings" not in data:
        raise ValueError(f"{path.name} 不是 paperatomy 的报告 JSON")
    return data


def compare_payload(a: dict, b: dict) -> dict:
    """对齐两份报告，返回渲染所需的极简 payload。"""
    fa = {f.get("block_id"): f for f in a.get("findings") or []}
    fb = {f.get("block_id"): f for f in b.get("findings") or []}
    order: list[str] = []
    for k in list(fa) + list(fb):
        if k not in order:
            order.append(k)
    rows = []
    for bid in order:
        ra, rb = fa.get(bid), fb.get(bid)
        rows.append({
            "block_id": bid,
            "label": (ra or rb or {}).get("label", bid),
            "a": ra,
            "b": rb,
            "only": "a" if rb is None else ("b" if ra is None else ""),
        })
    return {
        "a": _meta(a), "b": _meta(b),
        "rows": rows,
        "chain_a": (a.get("chain") or {}).get("nodes") or [],
        "chain_b": (b.get("chain") or {}).get("nodes") or [],
    }


def _tpl_name(t) -> str:
    """template 字段兼容 dict（{id,name,…}）与字符串。"""
    if isinstance(t, dict):
        return str(t.get("id") or t.get("name") or "")
    return str(t or "")


def _meta(d: dict) -> dict:
    return {
        "title": d.get("paper_title") or "(未识别标题)",
        "template": _tpl_name(d.get("template")),
        "lang": d.get("lang", ""),
        "blocks": len(d.get("findings") or []),
        "chain": len(((d.get("chain") or {}).get("nodes")) or []),
        "network": len(((d.get("network") or {}).get("nodes")) or []),
        "source": d.get("source", ""),
    }


def _ev_html(evs: list) -> str:
    out = []
    for e in evs or []:
        loc = "§%s·第%s段·p.%s" % (
            e.get("section_no") or "?", e.get("para") or "?", e.get("page") or "?")
        ver = "" if e.get("verified") else ' <span class="unver">未核实</span>'
        q = html.escape(str(e.get("quote") or "")[:120])
        zh = html.escape(str(e.get("quote_zh") or "")[:120])
        out.append('<div class="ev"><span class="loc">%s</span>%s '
                   '<span class="q">%s</span>'
                   '<span class="zh">%s</span></div>'
                   % (html.escape(loc), ver, q, zh))
    return "".join(out)


def to_compare_html(payload: dict, title: str = "论文对比") -> str:
    """渲染单文件对照 HTML。样式与报告同族（浅色）。"""
    A, B = payload["a"], payload["b"]
    h = ['<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">']
    h.append("<title>%s</title>" % html.escape(title))
    h.append("""<style>
:root { --bg:#FAF8F4; --card:#fff; --ink:#1B2430; --ink2:#56616F;
        --line:#E7E2D8; --accent:#2E6F8E; --soft:#E7F1F6; --warn:#B8703F; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
       font:15px/1.75 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }
.wrap { max-width:1280px; margin:0 auto; padding:28px 26px 60px; }
h1 { font-size:22px; margin:0 0 4px; }
.sub { color:var(--ink2); font-size:13px; margin-bottom:22px; }
.meta { display:flex; gap:14px; margin-bottom:24px; }
.mcard { flex:1; background:var(--card); border:1px solid var(--line);
         border-radius:12px; padding:14px 18px; }
.mcard h2 { font-size:16px; margin:0 0 6px; color:var(--accent); }
.mcard .kv { font-size:13px; color:var(--ink2); }
table { width:100%; border-collapse:collapse; background:var(--card);
        border:1px solid var(--line); border-radius:12px; overflow:hidden; }
th, td { padding:12px 14px; vertical-align:top; text-align:left;
         border-bottom:1px solid var(--line); }
th { background:var(--soft); font-size:13px; color:var(--ink2); font-weight:600; }
td.blk { width:120px; font-weight:600; font-size:13px; }
td.blk small { display:block; color:var(--ink3,#8A94A2); font-weight:400; }
td.col { width:41%; font-size:14px; }
td.only-a { background:#FBF1E6; } td.only-b { background:#E7F1F6; }
.none { color:var(--ink3,#8A94A2); font-style:italic; }
.ev { margin-top:8px; font-size:12px; color:var(--ink2);
      border-left:2px solid var(--line); padding-left:8px; }
.ev .loc { color:var(--accent); font-family:ui-monospace,Consolas,monospace; }
.ev .unver { color:var(--warn); }
.ev .q { display:block; }
.ev .zh { display:block; color:var(--ink); }
.chainbox { background:var(--card); border:1px solid var(--line); border-radius:12px;
            padding:16px 18px; margin-top:24px; }
.chainbox h2 { font-size:16px; margin:0 0 10px; color:var(--accent); }
.steps { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
.step { font-size:12.5px; padding:3px 10px; border-radius:999px;
        background:var(--soft); color:var(--ink); }
.arrow { color:var(--ink2); }
.chain2col { display:flex; gap:18px; flex-wrap:wrap; }
.chain2col > div { flex:1; min-width:420px; }
.foot { margin-top:26px; color:var(--ink2); font-size:12.5px; }
</style></head><body><div class="wrap">""")
    h.append("<h1>%s</h1>" % html.escape(title))
    h.append('<div class="sub">两份报告逐板块并排 · 只呈现已有数据，不生成新判断 · '
             '生成命令 <code>paperatomy compare a.json b.json</code></div>')

    h.append('<div class="meta">')
    for tag, m in (("A", A), ("B", B)):
        h.append('<div class="mcard"><h2>%s · %s</h2><div class="kv">'
                 '模板 %s · %d 块 · 论证链 %d 节点 · 网络 %d 节点 · %s</div></div>'
                 % (tag, html.escape(str(m["title"])[:46]), html.escape(m["template"]),
                    m["blocks"], m["chain"], m["network"],
                    html.escape(m.get("source") or "—")))
    h.append("</div>")

    h.append("<table><tr><th>板块</th><th>A · %s</th><th>B · %s</th></tr>"
             % (html.escape(str(A["title"])[:24]), html.escape(str(B["title"])[:24])))
    for r in payload["rows"]:
        cls = " class=\"only-a\"" if r["only"] == "a" else \
              (" class=\"only-b\"" if r["only"] == "b" else "")
        h.append("<tr%s>" % cls)
        h.append('<td class="blk">%s<small>%s%s</small></td>'
                 % (html.escape(r["label"]), html.escape(r["block_id"]),
                    (" · 仅A" if r["only"] == "a" else
                     " · 仅B" if r["only"] == "b" else "")))
        for side in ("a", "b"):
            f = r[side]
            if not f:
                h.append('<td class="col"><span class="none">此报告无此板块</span></td>')
                continue
            conf = f.get("confidence", "")
            h.append('<td class="col">%s%s%s</td>' % (
                html.escape(str(f.get("content") or "")),
                '<div class="ev" style="margin-top:6px;color:#8A94A2">置信度 %s</div>'
                % html.escape(str(conf)) if conf else "",
                _ev_html(f.get("evidence") or [])))
        h.append("</tr>")
    h.append("</table>")

    h.append('<div class="chainbox"><h2>论证链对照</h2><div class="chain2col">')
    for tag, chain in (("A", payload["chain_a"]), ("B", payload["chain_b"])):
        h.append("<div><b>%s</b><div class=\"steps\"></div>" % tag)
        if not chain:
            h.append('<div class="none" style="margin-top:8px">没有可演的链</div>')
        steps = []
        for n in chain:
            steps.append('<span class="step">%s · %s</span>'
                         % (html.escape(n.get("type", "")),
                            html.escape(str(n.get("label") or "")[:22])))
        h.append('<div class="steps">%s</div></div>'
                 % '<span class="arrow">→</span>'.join(steps))
    h.append("</div></div>")

    h.append('<div class="foot">对照页由程序合成（compare.py），内容全部来自两份'
             '报告 JSON；证据定位与「未核实」标记的含义与报告内一致。</div>')
    h.append("</div></body></html>")
    return "".join(h)


def run_compare(path_a: str, path_b: str, output: str | None = None) -> int:
    pa, pb = Path(path_a), Path(path_b)
    for p in (pa, pb):
        if not p.is_file():
            print(f"✗ 找不到文件：{p}")
            return 1
    a, b = _load(pa), _load(pb)
    if _tpl_name(a.get("template")) != _tpl_name(b.get("template")):
        print("⚠ 两份报告模板不同（%s vs %s），板块对不齐时以「仅A/仅B」标注"
              % (_tpl_name(a.get("template")), _tpl_name(b.get("template"))))
    payload = compare_payload(a, b)
    title = "对比：%s vs %s" % (payload["a"]["title"], payload["b"]["title"])
    html_out = to_compare_html(payload, title)
    prefix = Path(output) if output else pa.with_suffix("")
    out_path = prefix.with_suffix(".compare.html")
    out_path.write_text(html_out, encoding="utf-8")
    print(f"已写出 {out_path}  ← 用浏览器打开这个")
    return 0


# ---------------------------------------------------------------------------
# N 篇对比（v0.13.0）：结构对齐 + 模型综合分析（分析由 serve 侧生成后传入）
# ---------------------------------------------------------------------------

def compare_many_payload(dicts: list) -> dict:
    """N 篇报告对齐：block_id 并集为行，每篇一列。"""
    metas = [_meta(d) for d in dicts]
    maps = [{f.get("block_id"): f for f in (d.get("findings") or [])}
            for d in dicts]
    order: list[str] = []
    for m in maps:
        for k in m:
            if k not in order:
                order.append(k)
    rows = []
    for bid in order:
        cells = [m.get(bid) for m in maps]
        have = sum(1 for x in cells if x is not None)
        label = bid
        for x in cells:
            if x:
                label = x.get("label", bid)
                break
        rows.append({"block_id": bid, "label": label,
                     "cells": cells, "only_one": have == 1})
    return {"metas": metas, "rows": rows}


def to_compare_html_many(payload: dict, title: str) -> str:
    """N 篇对比页：模型综合分析置顶 + 结构对齐表。"""

    def esc(x) -> str:
        return html.escape(str(x or ""))

    metas = payload.get("metas") or []
    rows = payload.get("rows") or []
    ana = payload.get("analysis") or {}
    n = len(metas)

    meta_cards = "".join(
        "<div class='m'><b>" + esc(m.get("title"))[:52] +
        "</b><div class='sub'>" + esc(m.get("template")) + " · " +
        str(m.get("blocks", 0)) + " 块 · 链 " + str(m.get("chain", 0)) +
        " · 网络 " + str(m.get("network", 0)) + "</div></div>"
        for m in metas)

    common = esc(ana.get("common") or "")
    verdict = esc(ana.get("verdict") or "")
    table = ana.get("table") or []
    parts = []
    if common:
        parts.append("<p class='lead'>" + common + "</p>")
    if table:
        head_row = "<tr><th style='width:110px'>维度</th>" + "".join(
            "<th>论文" + str(i + 1) + "</th>" for i in range(n)) + "</tr>"
        trows = ""
        for t in table:
            cells = t.get("cells") or []
            tds = "".join(
                "<td>" + esc(cells[i] if i < len(cells) else "—") + "</td>"
                for i in range(n))
            trows += "<tr><th>" + esc(t.get("dim")) + "</th>" + tds + "</tr>"
        parts.append("<table>" + head_row + trows + "</table>")
    if verdict:
        parts.append("<p class='lead'><b>选读建议：</b>" + verdict + "</p>")
    ana_html = ("<div class='sec'><h2>模型综合对比</h2>" + "".join(parts) +
                "</div>") if parts else (
        "<div class='sec'><p class='lead'>模型综合分析未生成（可在工作台重试）。</p></div>")

    align_head = "<tr><th style='width:130px'>板块</th>" + "".join(
        "<th>论文" + str(i + 1) + "</th>" for i in range(n)) + "</tr>"
    align_rows = ""
    for r in rows:
        cells_html = ""
        for x in (r.get("cells") or []):
            if x is None:
                cells_html += "<td class='na'>—</td>"
            else:
                body = str(x.get("content") or "")
                more = "…" if len(body) > 260 else ""
                cells_html += "<td>" + esc(body[:260]) + more + "</td>"
        tag = "<span class='tag'>仅一篇</span>" if r.get("only_one") else ""
        align_rows += "<tr><th>" + esc(r.get("label")) + tag + "</th>" + cells_html + "</tr>"

    tpl = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>__TITLE__</title>"
        "<style>"
        "body{font-family:'Segoe UI',system-ui,sans-serif;max-width:1080px;"
        "margin:26px auto;padding:0 18px;color:#24303B;background:#FBFAF7}"
        "h1{font-size:20px}h2{font-size:16px;margin:0 0 10px}"
        ".meta{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0 6px}"
        ".m{border:1px solid #E7E2D8;border-radius:10px;padding:10px 14px;"
        "flex:1;min-width:220px;background:#fff}"
        ".m .sub{font-size:12px;color:#56616F;margin-top:4px}"
        "table{border-collapse:collapse;width:100%;font-size:13.5px;"
        "background:#fff;margin-top:8px}"
        "td,th{border:1px solid #E7E2D8;padding:8px 10px;vertical-align:top;"
        "text-align:left;line-height:1.55}"
        "th{background:#F0EDE5;font-weight:600;white-space:nowrap}"
        "td.na{color:#B9B2A4;text-align:center}"
        ".sec{margin:24px 0;background:#fff;border:1px solid #E7E2D8;"
        "border-radius:12px;padding:16px 18px}"
        ".lead{font-size:14px;line-height:1.7;margin:8px 0}"
        ".tag{display:inline-block;background:rgba(46,111,142,.1);"
        "color:#2E6F8E;border-radius:6px;padding:1px 8px;font-size:11px;"
        "margin-left:6px;font-weight:400}"
        ".back{font-size:13px;color:#2E6F8E;text-decoration:none}"
        "</style></head><body>"
        "<a class='back' href='javascript:history.back()'>← 返回</a>"
        "<h1>__TITLE__</h1>"
        "<div class='meta'>__META__</div>"
        "__ANA__"
        "<div class='sec'><h2>板块对齐（逐块对照）</h2>"
        "<table>__AHEAD____AROWS__</table>"
        "</div></body></html>")
    return (tpl.replace("__TITLE__", esc(title))
               .replace("__META__", meta_cards)
               .replace("__ANA__", ana_html)
               .replace("__AHEAD__", align_head)
               .replace("__AROWS__", align_rows))
