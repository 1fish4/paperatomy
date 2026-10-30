"""PDF 解析的测试。

用程序生成 PDF 来测，而不是依赖一份固定样本：
生成的样本可以精确控制双栏位置、章节标题、页眉页脚，
才能测到「解析器到底有没有正确处理」而不是「这份样本碰巧能过」。
"""

from __future__ import annotations

import pytest

fitz = pytest.importorskip("fitz", reason="需要 PyMuPDF")

from paperatomy.parse import Page, Paper, Section, parse


def _make_pdf(path, *, pages: int = 2, two_column: bool = False) -> str:
    """生成一份最小可解析的 PDF。

    参数
    ----
    two_column : bool
        为真时，每页左右两栏各写一组编号段落，用于验证阅读顺序还原。
    """
    doc = fitz.open()

    for i in range(pages):
        page = doc.new_page(width=595, height=842)

        if i == 0:
            page.insert_text((60, 90), "A Survey on Widget Systems",
                             fontsize=20)
            page.insert_text((60, 130), "Abstract", fontsize=12)
            page.insert_text((60, 150),
                             "This survey reviews widget systems and their limits.",
                             fontsize=10)

        # 页眉与页脚：解析时应被过滤
        page.insert_text((60, 30), "Journal of Widgets, Vol. 1", fontsize=8)
        page.insert_text((60, 820), "Page %d" % (i + 1), fontsize=8)

        if two_column:
            for k in range(4):
                y = 200 + k * 70
                page.insert_text((60, y), f"L{k} left column body text.",
                                 fontsize=10)
                page.insert_text((320, y), f"R{k} right column body text.",
                                 fontsize=10)
        else:
            for k in range(4):
                page.insert_text((60, 200 + k * 40),
                                 f"Body line {k} of page {i + 1}.", fontsize=10)

        if i == 1:
            page.insert_text((60, 200), "1 Introduction", fontsize=13)
            page.insert_text((60, 240), "Intro body text here.", fontsize=10)
            page.insert_text((60, 420), "2 Methods", fontsize=13)
            page.insert_text((60, 460), "Methods body text here.", fontsize=10)

    doc.save(path)
    doc.close()
    return str(path)


# ---------------------------------------------------------------------------
# 基础解析
# ---------------------------------------------------------------------------

def test_parse_reports_page_count(tmp_path):
    pdf = _make_pdf(tmp_path / "a.pdf", pages=3)
    paper = parse(pdf)
    assert paper.total_pages == 3
    assert isinstance(paper, Paper)


def test_parse_keeps_page_numbering(tmp_path):
    """页码必须从 1 开始连续，evidence 靠它定位。"""
    pdf = _make_pdf(tmp_path / "a.pdf", pages=2)
    paper = parse(pdf)
    assert [p.number for p in paper.pages] == [1, 2]


def test_parse_extracts_text(tmp_path):
    pdf = _make_pdf(tmp_path / "a.pdf")
    paper = parse(pdf)
    assert "widget" in paper.pages[0].text.lower()


def test_parse_guesses_title(tmp_path):
    pdf = _make_pdf(tmp_path / "a.pdf")
    paper = parse(pdf)
    assert "Survey" in paper.title or "Widget" in paper.title


def test_parse_filters_running_header_and_footer(tmp_path):
    """页眉页脚不该混进正文，否则会被当成内容送给模型。"""
    pdf = _make_pdf(tmp_path / "a.pdf", pages=2)
    paper = parse(pdf)
    second = paper.pages[1].text
    assert "Journal of Widgets" not in second
    assert "Page 2" not in second


# ---------------------------------------------------------------------------
# 双栏还原
# ---------------------------------------------------------------------------

def test_two_column_reading_order(tmp_path):
    """双栏页必须左栏读完再读右栏，不能左右交错。"""
    pdf = _make_pdf(tmp_path / "two.pdf", pages=1, two_column=True)
    paper = parse(pdf)
    text = paper.pages[0].text

    left_idx = text.find("L3 left")
    right_idx = text.find("R0 right")
    assert left_idx != -1 and right_idx != -1
    assert left_idx < right_idx, "右栏内容出现在左栏之前，说明栏序还原失败"


def test_single_column_not_split(tmp_path):
    """单栏页面不能被误判成双栏打乱顺序。"""
    pdf = _make_pdf(tmp_path / "one.pdf", pages=1, two_column=False)
    paper = parse(pdf)
    text = paper.pages[0].text
    assert text.find("Body line 0") < text.find("Body line 3")


# ---------------------------------------------------------------------------
# 章节与压缩
# ---------------------------------------------------------------------------

def test_sections_detected_from_heading_lines(tmp_path):
    pdf = _make_pdf(tmp_path / "a.pdf", pages=2)
    paper = parse(pdf)
    titles = " ".join(s.title for s in paper.sections)
    assert paper.sections, "没有识别出任何章节"
    assert "Introduction" in titles or "Methods" in titles


def test_full_text_contains_page_markers(tmp_path):
    pdf = _make_pdf(tmp_path / "a.pdf", pages=2)
    paper = parse(pdf)
    assert "[page 1]" in paper.full_text()
    assert "[page 2]" in paper.full_text()


def test_condensed_respects_budget(tmp_path):
    """压缩后的长度必须受控，否则会撑爆上下文。"""
    pdf = _make_pdf(tmp_path / "a.pdf", pages=4)
    paper = parse(pdf)
    out = paper.condensed(max_chars=2000)
    assert len(out) <= 2600, f"压缩后 {len(out)} 字符，超出预算"


def test_condensed_short_paper_is_untouched(tmp_path):
    """短论文不该被压缩，保持原文。"""
    pdf = _make_pdf(tmp_path / "a.pdf", pages=1)
    paper = parse(pdf)
    assert paper.condensed(max_chars=10_000) == paper.full_text()


# ---------------------------------------------------------------------------
# 参考文献
# ---------------------------------------------------------------------------

def _make_pdf_with_references(path):
    """正文 + 一大段参考文献，模拟真实综述的结构。"""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 80), "1 Introduction", fontsize=13)
    page.insert_text((60, 110), "Body text about the actual content.", fontsize=10)

    page2 = doc.new_page(width=595, height=842)
    page2.insert_text((60, 80), "References", fontsize=13)
    for k in range(20):
        page2.insert_text((60, 110 + k * 25),
                          f"[{k}] Author {k}. Some cited paper title. 2024.",
                          fontsize=9)
    doc.save(path)
    doc.close()
    return str(path)


def test_reference_section_is_flagged(tmp_path):
    paper = parse(_make_pdf_with_references(tmp_path / "ref.pdf"))
    marked = [s for s in paper.sections if s.is_reference]
    assert len(marked) == 1
    assert "reference" in marked[0].title.lower()


def test_condensed_excludes_reference_body(tmp_path):
    """参考文献不能占抽取预算——实测一篇综述里它能占 38%。"""
    paper = parse(_make_pdf_with_references(tmp_path / "ref.pdf"))
    out = paper.condensed(max_chars=500)
    assert "[skipped]" in out
    assert "Some cited paper title" not in out
    assert "Body text about the actual content" in out


def test_condensed_keeps_section_tail():
    """长章节压缩时必须保留结尾。

    只留开头会丢掉章节末尾的小结与最后一条结论——
    实测一篇综述的结论章节第 5 个未来方向正好被切掉，抽取少了一条。
    （短章节现在直接全量保留，所以这里要用一个超过 6000 字符的章节来测。）
    """
    head = "HEAD_MARKER " + ("开头内容。" * 800)
    tail = ("结尾内容。" * 800) + " TAIL_MARKER"
    paper = Paper(
        title="t",
        pages=[Page(number=1, text=head + tail)],
        sections=[Section(title="长章节", start_page=1, text=head + tail)],
    )

    out = paper.condensed(max_chars=1200)
    assert "HEAD_MARKER" in out, "开头被丢了"
    assert "TAIL_MARKER" in out, "结尾被丢了"
    assert "中间" in out and "省略" in out, "缺少省略提示"


def test_condensed_keeps_short_section_whole():
    """短章节（摘要、结论这类）必须一字不改地保留。"""
    body = "短章节内容。" * 200  # 约 1200 字符，低于 6000 阈值
    paper = Paper(
        title="t",
        pages=[Page(number=1, text=body)],
        sections=[Section(title="结论", start_page=1, text=body)],
    )
    out = paper.condensed(max_chars=800)
    assert "省略" not in out
    assert out.count("短章节内容。") == 200


# ---------------------------------------------------------------------------
# 错误处理
# ---------------------------------------------------------------------------

def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse(tmp_path / "nope.pdf")


def test_non_pdf_raises(tmp_path):
    bad = tmp_path / "not.pdf"
    bad.write_text("这不是 PDF", encoding="utf-8")
    with pytest.raises(ValueError):
        parse(bad)
