"""PDF 解析：把论文变成「带坐标的章节文本」。

为什么页码是核心
----------------
抽取结果的每个板块都要给出证据（出处 + 原文摘录）。
所以解析阶段**不能把全文拼成一整块** —— 定位信息一旦丢失，
后面就没法告诉用户「这句话在原文第几页」。

这也是本工具与「把 PDF 丢给模型」的根本区别：不是喂一坨文本，是喂带定位的结构。

为什么还要段号
--------------
只给页码仍然不够用：读者拿到「p.11」还得在一整页两栏里自己找那一句。
而让模型自己报「第 4.2 节第 3 段」它数不准——它看到的文本没有段的边界。

所以这里多做一件事：解析时就把全文切成段落并编号（:class:`Para`）。
模型只负责给出引文，由 :mod:`paperatomy.locate` 拿引文回来匹配，
**命中哪段就报哪段的坐标**。定位精度与「引文是否真实存在」
因此变成同一个动作的副产品——编造的引文根本匹配不上。

双栏怎么处理
------------
学术论文大多双栏。PyMuPDF 的 ``get_text()`` 按内容流顺序输出，
双栏时会把左右栏交错串在一起，读起来是乱的。
这里按 x 坐标把文本块分到左右两栏，各栏内按 y 排序，再拼接。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

try:  # PyMuPDF 2.0 起正式模块名是 pymupdf，fitz 仅作兼容别名保留
    import pymupdf as fitz
except ImportError:  # 老版本只有 fitz
    import fitz

__all__ = ["Page", "Section", "Para", "Paper", "parse", "norm_text"]


#: 章节标题：``1 Introduction`` / ``2.1 Datasets`` / ``3.2.1 Model``
_SECTION_RE = re.compile(
    r"^[ \t]*(\d{1,2}(?:\.\d{1,2}){0,2})[ \t\.]+"
    r"([A-Z][A-Za-z0-9 ,\-:&'/]{2,70})[ \t]*$",
    re.MULTILINE,
)

#: 参考文献章节。占篇幅很大（实测一篇 35 页的综述里占 38%），
#: 但对抽取研究内容没有价值，压缩时必须排除。
_REFERENCE_RE = re.compile(
    r"^\s*(references|bibliography|literature\s+cited|参考文献|引用文献)\s*$",
    re.IGNORECASE,
)

#: 页眉页脚判定：位于页面上下边缘、且很短的块直接丢弃
_EDGE_RATIO = 0.065
_EDGE_MAX_CHARS = 90


@dataclass
class Page:
    """一页的内容。"""

    number: int
    """页码，从 1 开始。"""

    text: str = ""

    def __bool__(self) -> bool:
        return bool(self.text.strip())


@dataclass
class Section:
    """一个章节。"""

    title: str
    start_page: int = 1
    text: str = ""
    is_reference: bool = False
    """是否为参考文献章节。这类章节不参与压缩抽取。"""

    number: str = ""
    """章节编号，如 ``4.2``。书签里拿不到时为空串。"""

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass
class Para:
    """一个段落，以及它在原文里的坐标。

    这是「证据能不能落到具体位置」的关键。段号由程序在解析时数出来，
    不依赖模型自报，所以对得上、可核对。
    """

    section: str = ""
    """所在章节标题。"""

    section_no: str = ""
    """章节编号，如 ``4.2``。"""

    no: int = 0
    """**本章内**第几段，从 1 开始。"""

    page: int = 0
    """起始页码。"""

    text: str = ""

    norm: str = ""
    """归一化后的文本，用于匹配。见 :func:`norm_text`。"""

    @property
    def locator(self) -> str:
        """人类可读的坐标，如 ``§4.2 · 第 3 段 · p.11``。"""
        head = f"§{self.section_no}" if self.section_no else "§"
        tail = f"第 {self.no} 段"
        return f"{head} · {tail} · p.{self.page}" if self.page else f"{head} · {tail}"

    @property
    def heading(self) -> str:
        """章节标题（不含编号），用于展示。"""
        title = re.sub(r"^\s*\d{1,2}(?:\.\d{1,2}){0,2}[\s\.]+", "", self.section)
        return title.strip()


def norm_text(text: str) -> str:
    """把文本归一化成可用于匹配的形式。

    只做**不改变语义**的变形，因为匹配成功后要给用户指认原文：

    - 大小写、软连字符、断行连字符（``resource-\\nefficient`` → ``resource efficient``）
    - 各种引号破折号统一
    - 所有非字母数字汉字的字符折叠成单个空格

    实测这一步能把双栏拼接造成的空格错位、PDF 拆行造成的连字符断裂都吃掉。
    """
    text = text.lower().replace("\u00ad", "")
    text = re.sub(r"-\s*\n\s*", " ", text)
    text = re.sub(r"[\u2018\u2019\u201b]", "'", text)
    text = re.sub(r"[\u201c\u201d]", '"', text)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff']+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


#: 短于这个长度的片段不单独成段（多半是公式编号、图注残片）。
_MIN_PARA_CHARS = 25

#: 一页里超过这么多行，就认为 PDF 把人眼的一段拆成了多行，需要按句子重新合并。
_LINE_SPLIT_THRESHOLD = 12


def _same_heading(line: str, title: str) -> bool:
    """这一行是不是章节标题本身（避免把标题算作第 1 段）。"""
    a = norm_text(line)
    b = norm_text(title)
    if not a or not b:
        return False
    return a == b or (a.endswith(b) and len(a) - len(b) <= 4)


def _split_paragraphs(text: str, skip_title: str = "") -> list[str]:
    """把一页文本切成段落。

    ``_page_text`` 输出的每个 ``\\n`` 分隔片段基本就是一个视觉块
    （PyMuPDF 的行合并结果），正常情况下一块即一段。
    少数页面被逐行拆开，那时片段数量会异常多，退回到「按句末标点合并」。
    """
    units = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if skip_title:
        units = [u for u in units if not _same_heading(u, skip_title)]

    if len(units) <= _LINE_SPLIT_THRESHOLD:
        chunks = units
    else:
        chunks: list[str] = []
        buf = ""
        for unit in units:
            buf = f"{buf} {unit}".strip() if buf else unit
            if buf.endswith((".", "!", "?", ":")):
                chunks.append(buf)
                buf = ""
        if buf:
            chunks.append(buf)

    return [c for c in chunks if len(c) >= _MIN_PARA_CHARS]


def _build_paragraphs(pages: list[Page], sections: list[Section]) -> list[Para]:
    """按章节切段落，给每一段编上「章节 + 段号 + 页码」。

    参考文献整节跳过：那里面每条都是一个短行，编成段号会把正文的段号顶得很大。
    """
    if not pages or not sections:
        return []

    out: list[Para] = []
    for i, sec in enumerate(sections):
        if sec.is_reference or not sec.text.strip():
            continue
        stop = sections[i + 1].start_page if i + 1 < len(sections) else len(pages) + 1
        start = max(1, min(sec.start_page, len(pages)))
        stop = min(max(stop, start + 1), len(pages) + 1)

        no = 0
        for pno in range(start, stop):
            raw = pages[pno - 1].text if 1 <= pno <= len(pages) else ""
            for chunk in _split_paragraphs(raw, skip_title=sec.title if no == 0 else ""):
                no += 1
                out.append(Para(
                    section=sec.title,
                    section_no=sec.number,
                    no=no,
                    page=pno,
                    text=chunk,
                    norm=norm_text(chunk),
                ))
    return out


@dataclass
class Paper:
    """解析后的论文。"""

    path: str = ""
    title: str = ""
    pages: list[Page] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    paragraphs: list[Para] = field(default_factory=list)
    """全文段落索引。证据定位靠它——引文匹配到哪段，就报哪段的坐标。"""

    @property
    def total_pages(self) -> int:
        return len(self.pages)

    @property
    def chars(self) -> int:
        return sum(len(p.text) for p in self.pages)

    def page_text(self, number: int) -> str:
        """取某一页的文本。页码越界时返回空串。"""
        if 1 <= number <= len(self.pages):
            return self.pages[number - 1].text
        return ""

    def full_text(self) -> str:
        """全文，页与页之间以页码标记分隔。

        页码标记保留在文本里，模型引用时才有据可依。
        """
        out: list[str] = []
        for page in self.pages:
            if page.text.strip():
                out.append(f"\n===== [page {page.number}] =====\n{page.text}")
        return "".join(out)

    def condensed(self, max_chars: int = 90_000) -> str:
        """按预算压缩全文，供模型阅读。

        为什么不直接截断
        ----------------
        综述论文常常 40 页以上，全文硬截到 6 万字符会把后半部分整个丢掉，
        而结论、开放问题往往就在最后。

        做法是按**章节加权分配预算**，每节取头 + 尾。
        这样每一节都能露出（章节骨架完整），只是细节被削掉。
        """
        if self.chars <= max_chars:
            return self.full_text()

        if not self.sections:
            return self.full_text()[:max_chars]

        # 参考文献不参与预算分配。一篇 35 页的综述里它可以占到 38%，
        # 而那部分对抽取毫无价值。
        usable = [s for s in self.sections if not s.is_reference] or self.sections

        # 按章节长度**加权**分配预算，而不是平均分配。
        # 平均分配会让最长的那章被砍得最狠——实测一篇综述里第四章占 43%，
        # 平均分之下只拿到 13% 的预算，服务算法与模型压缩整节丢失。
        overhead = sum(len(s.title) + 40 for s in usable)
        budget = max(max_chars - overhead, 4_000)

        # 短章节直接全量保留。摘要、引言、结论这类章节信息密度最高，
        # 按比例砍掉一点点，代价往往是「少一条结论」——
        # 实测综述的结论章节被砍 13%，第 5 个未来方向就没了。
        SHORT_KEEP = 6_000
        short = [s for s in usable if len(s.text) <= SHORT_KEEP]
        long_ = [s for s in usable if len(s.text) > SHORT_KEEP]

        alloc: dict[int, int] = {}
        used = 0
        for s in short:
            alloc[id(s)] = len(s.text)
            used += len(s.text)

        remaining = max(budget - used, 4_000)
        long_total = sum(len(s.text) for s in long_) or 1
        for s in long_:
            alloc[id(s)] = max(int(remaining * len(s.text) / long_total), 600)

        out: list[str] = []
        skipped = [s for s in self.sections if s.is_reference]
        if skipped:
            names = "、".join(s.title for s in skipped)
            out.append(f"\n===== [skipped] {names}（{sum(s.chars for s in skipped):,} "
                       f"字符，与抽取无关，已省略）=====")

        for sec in usable:
            limit = alloc[id(sec)]
            body = sec.text.strip()
            if len(body) > limit:
                # 保留「头 + 尾」，不是只留开头。
                # 章节末尾常放小结与最后一条结论——实测综述的结论章节
                # 第 5 个未来方向（隐私保护）正好被纯截断切掉，导致抽取少一条。
                head_len = int(limit * 0.68)
                tail_len = max(limit - head_len - 30, 200)
                head, tail = body[:head_len], body[-tail_len:]

                # 头向前收、尾向后收，尽量落在句子边界
                for sep in ("。 ", ". ", "\n"):
                    pos = head.rfind(sep)
                    if pos > head_len * 0.75:
                        head = head[: pos + 1]
                        break
                for sep in ("。 ", ". ", "\n"):
                    pos = tail.find(sep)
                    if 0 <= pos < tail_len * 0.25:
                        tail = tail[pos + 1:].lstrip()
                        break

                omitted = len(body) - len(head) - len(tail)
                body = (f"{head}\n…（中间 {omitted:,} 字符省略）…\n{tail}"
                        if omitted > 0 else body)
            out.append(f"\n===== [section] {sec.title} "
                       f"(p.{sec.start_page}) =====\n{body}")
        return "".join(out)

    def summary_block(self) -> str:
        """首页（标题、摘要、引言开头），信息密度最高的一段。"""
        if not self.pages:
            return ""
        head = self.pages[0].text
        return head[:6_000]


# ---------------------------------------------------------------------------
# 解析实现
# ---------------------------------------------------------------------------

def _page_units(page: "fitz.Page", *, is_first: bool) -> list[tuple[float, float, str]]:
    """取一页的**阅读单元**，过滤页眉页脚，返回 ``(x0, y0, text)``。

    为什么降到行级
    --------------
    PyMuPDF 的 ``get_text("blocks")`` 有时会把**同一基线上的左右两栏合并成一个块**
    （块文本变成 ``"L0 ...\\nR0 ..."``，x0 到 x1 横跨两栏）。
    按块分栏在这种情况下完全失效，只能降到 line 粒度：
    块内的每一行各自按 x 判断属于哪一栏。
    """
    try:
        data = page.get_text("dict")
    except Exception:
        return [(0.0, 0.0, page.get_text())]

    height = page.rect.height or 1.0
    width = page.rect.width or 1.0
    mid = width / 2
    top_limit = height * _EDGE_RATIO
    bottom_limit = height * (1 - _EDGE_RATIO)

    units: list[tuple[float, float, str]] = []

    for block in data.get("blocks", []):
        if block.get("type", 0) != 0:  # 1 是图片
            continue
        bx0, by0, _bx1, by1 = block.get("bbox", (0.0, 0.0, 0.0, 0.0))

        lines: list[tuple[float, float, float, str]] = []
        for line in block.get("lines", []):
            parts = [
                s.get("text", "")
                for s in line.get("spans", [])
                if s.get("text", "").strip()
            ]
            text = "".join(parts).strip()
            if not text:
                continue
            lx0, ly0, lx1, _ly1 = line.get("bbox", (0.0, 0.0, 0.0, 0.0))
            lines.append((float(lx0), float(ly0), float(lx1), text))

        if not lines:
            continue

        # 页眉页脚：位于页面上下边缘且整块很短。首页不过滤，标题常贴顶部
        block_text = " ".join(l[3] for l in lines)
        if not is_first and len(block_text) < _EDGE_MAX_CHARS:
            if by1 < top_limit or by0 > bottom_limit:
                continue

        left = [l for l in lines if l[2] <= mid]
        right = [l for l in lines if l[0] >= mid]
        crossing = len(lines) - len(left) - len(right)

        if crossing == 0 and left and right:
            # 一个块里同时含左右两栏：逐行拆开，各自归位
            for lx0, ly0, _lx1, text in lines:
                units.append((lx0, ly0, text))
        else:
            # 同一栏内的折行段落：合并成一段
            units.append((bx0, by0, " ".join(l[3] for l in lines)))

    return units


def _page_text(page: "fitz.Page", *, is_first: bool) -> str:
    """按栏还原一页的阅读顺序。"""
    units = _page_units(page, is_first=is_first)
    if not units:
        return ""

    width = page.rect.width or 1.0
    mid = width / 2
    left = [u for u in units if u[0] < mid]
    right = [u for u in units if u[0] >= mid]

    # 两侧都有多个单元才认定为双栏，否则按单栏处理，避免误伤
    if len(left) >= 2 and len(right) >= 2:
        ordered = sorted(left, key=lambda u: u[1]) + sorted(right, key=lambda u: u[1])
    else:
        ordered = sorted(units, key=lambda u: (u[1], u[0]))

    return "\n".join(u[2] for u in ordered)


def _guess_title(doc: "fitz.Document", first_page_text: str) -> str:
    """猜标题：先看元数据，再看首页最大字号的文本。"""
    meta_title = (doc.metadata or {}).get("title", "") or ""
    meta_title = meta_title.strip()
    if 8 <= len(meta_title) <= 220:
        return meta_title

    # 首页字号最大的那一行
    try:
        page = doc[0]
        spans = []
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "").strip()
                    if text:
                        spans.append((span.get("size", 0), text))
        if spans:
            biggest = max(spans, key=lambda s: s[0])[0]
            picked = [t for size, t in spans if size >= biggest - 0.5]
            title = " ".join(picked).strip()
            if 8 <= len(title) <= 220:
                return title
    except Exception:
        pass

    # 退化为首页第一行较长的文本
    for line in first_page_text.splitlines():
        line = line.strip()
        if 12 <= len(line) <= 200:
            return line
    return ""


def _section_number(title: str) -> str:
    """从章节标题里抠出编号。``4.2 Resource-Efficient Attention`` → ``4.2``。"""
    m = re.match(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,2})[\s\.]+", title or "")
    return m.group(1) if m else ""


def _sections_from_toc(doc: "fitz.Document", pages: list[Page]) -> list[Section]:
    """用 PDF 书签切分章节。"""
    toc = doc.get_toc() or []
    entries = [
        (int(lvl), str(title).strip(), int(page))
        for lvl, title, page in toc
        if int(lvl) == 1 and str(title).strip()
    ]
    if len(entries) < 2:
        return []

    sections: list[Section] = []
    for i, (_lvl, title, start) in enumerate(entries):
        end = entries[i + 1][2] if i + 1 < len(entries) else len(pages) + 1
        start = max(1, min(start, len(pages)))
        end = max(start + 1, min(end, len(pages) + 1))
        body = "\n".join(
            pages[n - 1].text for n in range(start, end) if 1 <= n <= len(pages)
        )
        if body.strip():
            sections.append(Section(
                title=title,
                start_page=start,
                text=body,
                is_reference=bool(_REFERENCE_RE.match(title)),
                number=_section_number(title),
            ))
    return sections


def _sections_from_text(pages: list[Page]) -> list[Section]:
    """没有书签时，用行首编号识别章节标题。

    参考文献单独处理：它通常**没有编号**（只写 References / 参考文献），
    走编号正则识别不到，但恰恰是必须识别出来的一节——
    实测它能占到全篇 38%。
    """
    hits: list[tuple[int, str, str]] = []  # (页码, 编号, 标题)

    for page in pages:
        for m in _SECTION_RE.finditer(page.text):
            num, title = m.group(1), m.group(2).strip()
            if _is_body_like(title):
                continue
            hits.append((page.number, num, title))

        for line in page.text.splitlines():
            stripped = line.strip()
            if stripped and _REFERENCE_RE.match(stripped):
                hits.append((page.number, "R", stripped))

    # 两章也是有效的划分（短论文、技术报告很常见），阈值不能定成 3
    if len(hits) < 2:
        return []

    hits.sort(key=lambda h: h[0])

    # 取顶层编号（1, 2, 3…）作为章节边界；参考文献用 "R" 标记，同样算顶层
    top = [h for h in hits if "." not in h[1]]
    if len(top) < 2:
        top = hits

    sections: list[Section] = []
    for i, (page_no, num, title) in enumerate(top):
        # 本章到下一章起始页**之前**结束。
        # 用 stop + 1 会把下一章的起始页整页算进本章——
        # 一页里同时有本章结尾和下一章标题时，就会把下一章的内容混进来
        # （实测：Introduction 的正文里混进了整页参考文献）。
        stop = top[i + 1][0] if i + 1 < len(top) else len(pages) + 1
        body = "\n".join(
            pages[n - 1].text for n in range(page_no, stop)
            if 1 <= n <= len(pages)
        )
        if body.strip():
            sections.append(Section(
                title=title,
                start_page=page_no,
                text=body,
                is_reference=bool(_REFERENCE_RE.match(title)),
                number=_section_number(title) or ("" if num == "R" else num),
            ))
    return sections


def _is_body_like(title: str) -> bool:
    """排除正文里形似标题的行（全小写、过长、像句子）。"""
    if len(title) > 60:
        return True
    if title.islower():
        return True
    # 含句末标点的多半是正文
    return any(ch in title for ch in "。；，,;")


def parse(path: str | Path) -> Paper:
    """解析一篇 PDF 论文。

    参数
    ----
    path : str 或 Path
        PDF 文件路径。

    返回
    ----
    Paper
        含逐页文本、章节划分、标题与段落索引。

    异常
    -----
    FileNotFoundError
        文件不存在。
    ValueError
        不是可解析的 PDF。
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"找不到文件：{path}")

    try:
        doc = fitz.open(str(path))
    except Exception as exc:
        raise ValueError(f"无法作为 PDF 打开：{path}（{exc}）") from exc

    try:
        pages: list[Page] = []
        for i in range(doc.page_count):
            page = doc[i]
            text = _page_text(page, is_first=(i == 0))
            pages.append(Page(number=i + 1, text=text))

        first_text = pages[0].text if pages else ""
        title = _guess_title(doc, first_text)

        sections = _sections_from_toc(doc, pages)
        if not sections:
            sections = _sections_from_text(pages)
        if not sections:
            sections = [Section(title="全文", start_page=1,
                                text="\n".join(p.text for p in pages))]
    finally:
        doc.close()

    return Paper(
        path=str(path),
        title=title,
        pages=pages,
        sections=sections,
        paragraphs=_build_paragraphs(pages, sections),
    )
