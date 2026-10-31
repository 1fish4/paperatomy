"""段落索引：段号必须由程序数出来，不能靠模型自报。

这里守两件事：
1. 正常页「一个视觉块 = 一段」，不会被 PDF 的折行拆碎
2. 被逐行拆开的页面要能按句子重新合并，否则段号会虚高十几倍
"""

from paperatomy.parse import (
    Page,
    Para,
    Section,
    _build_paragraphs,
    _section_number,
    _split_paragraphs,
)


def test_section_number_extracted():
    assert _section_number("4 RESOURCE-EFFICIENT ALGORITHMS") == "4"
    assert _section_number("3.2 Attention") == "3.2"
    assert _section_number("Abstract") == ""
    assert _section_number("") == ""


def test_split_normal_page_keeps_blocks():
    page = (
        "Transformer models dominate the landscape of foundation models today, "
        "and their cost grows quadratically.\n"
        "Attention is the bottleneck because every token attends to every other token.\n"
        "Sparse attention reduces this to near linear time in practice."
    )
    chunks = _split_paragraphs(page)
    assert len(chunks) == 3
    assert chunks[0].startswith("Transformer")
    assert chunks[2].endswith("practice.")


def test_split_line_broken_page_merges_sentences():
    # 逐行拆开的页面：一个长句被 PDF 拆成 20+ 行，每行只有一两个词、不带句末标点，
    # 只有最后一行有句号。行数超过逐行判定阈值（12）时应触发合并，回到一段。
    sentence = ("the attention mechanism inside large foundation models faces "
                "severe computational bottlenecks because every token must "
                "attend to every other token in the sequence")
    words = sentence.split()
    lines = list(words)          # 每行一词，23 行，稳超阈值
    lines[-1] += "."
    assert len(lines) > 12, "用例本身必须超过逐行判定阈值，否则测不到合并分支"
    chunks = _split_paragraphs("\n".join(lines))
    assert len(chunks) == 1
    assert chunks[0].endswith("sequence.")


def test_split_drops_tiny_fragments():
    page = "12\n[1]\nA real paragraph that is long enough to survive the filter."
    chunks = _split_paragraphs(page)
    assert len(chunks) == 1
    assert chunks[0].startswith("A real paragraph")


def test_split_skips_section_heading():
    page = "4 RESOURCE-EFFICIENT ALGORITHMS\nA real paragraph long enough to be kept as a paragraph."
    chunks = _split_paragraphs(page, skip_title="4 RESOURCE-EFFICIENT ALGORITHMS")
    assert len(chunks) == 1
    assert chunks[0].startswith("A real paragraph")


def test_paragraph_numbers_restart_per_section():
    p1 = Page(number=1, text="Alpha section body text that is long enough to count as one paragraph.")
    p2 = Page(number=2, text="Beta section body text that is also long enough to count as one.")
    sections = [
        Section(title="1 Alpha", number="1", start_page=1, text=p1.text),
        Section(title="2 Beta", number="2", start_page=2, text=p2.text),
    ]
    paras = _build_paragraphs([p1, p2], sections)
    assert [p.no for p in paras] == [1, 1]
    assert [p.section_no for p in paras] == ["1", "2"]
    assert [p.page for p in paras] == [1, 2]


def test_reference_section_is_skipped():
    body = Page(number=1, text="Main body text that is long enough to be a paragraph.")
    refs = Page(number=2, text="[1] A. Author. A paper title. In Proc. of Something, 2023.")
    sections = [
        Section(title="1 Introduction", number="1", start_page=1, text=body.text),
        Section(title="References", start_page=2, text=refs.text, is_reference=True),
    ]
    paras = _build_paragraphs([body, refs], sections)
    assert len(paras) == 1
    assert paras[0].section_no == "1"


def test_no_sections_gives_no_paragraphs():
    assert _build_paragraphs([Page(number=1, text="x")], []) == []


def test_para_locator_format():
    p = Para(section="4 X", section_no="4", no=3, page=11)
    assert p.locator == "§4 · 第 3 段 · p.11"


def test_para_heading_strips_number():
    assert Para(section="4.2 Resource Attention", no=1).heading == "Resource Attention"


def test_para_heading_without_number():
    assert Para(section="Abstract", no=1).heading == "Abstract"
