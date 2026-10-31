"""证据定位与段号：把「p.13」细化成「§4.2 · 第 3 段 · p.11」。

这批测试守的是三件事：
1. 归一化足够宽容（大小写、断行连字符、双栏空格错位）
2. 但**不宽容到把编造的引文放进来**——匹配不上必须判未核实
3. 定位出的页码要覆盖模型自报的页码（后者常偏）
"""

from paperatomy.locate import Hit, annotate, locate_quote
from paperatomy.model import Evidence
from paperatomy.parse import Para, norm_text


def _para(no: int, page: int, text: str, *,
          section: str = "4 RESOURCE-EFFICIENT ALGORITHMS",
          section_no: str = "4") -> Para:
    return Para(section=section, section_no=section_no, no=no, page=page,
                text=text, norm=norm_text(text))


PARAS = [
    _para(1, 9, "Pre-training for large FMs relies on a substantial amount of "
                "computation resources. For instance, GPT-3 175B consumes "
                "3.14x10^23 FLOPs and LLaMA-70B takes 1.7x10^6 GPU hours."),
    _para(2, 9, "The attention mechanism in large FMs faces significant "
                "computational bottlenecks primarily due to its quadratic complexity."),
    _para(3, 10, "Speculative decoding replaces sequential token generation with "
                 "a draft-then-verify scheme, reporting a 2-3x improvement in "
                 "decoding throughput on the T5X model."),
]


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

def test_norm_lowercases_and_collapses_space():
    assert norm_text("Hello   World") == "hello world"


def test_norm_joins_hyphenated_linebreak():
    # PDF 拆行会把 resource-efficient 拆成 "resource-\nefficient"
    assert norm_text("resource-\nefficient") == "resource efficient"


def test_norm_keeps_chinese():
    assert norm_text("注意力机制的开销") == "注意力机制的开销"


def test_norm_strips_punctuation():
    assert norm_text("O(T^2 D), i.e. quadratic!") == "o t 2 d i e quadratic"


def test_norm_unifies_quotes():
    # 印刷体的弯引号和直引号要视为同一字符，否则摘录永远匹配不上
    assert norm_text("the \u201cattention\u201d block") == norm_text('the "attention" block')


# ---------------------------------------------------------------------------
# 定位
# ---------------------------------------------------------------------------

def test_full_quote_hits():
    hit = locate_quote(
        "Speculative decoding replaces sequential token generation with a "
        "draft-then-verify scheme", PARAS)
    assert hit.ok and hit.how == "full"
    assert hit.para.no == 3 and hit.para.page == 10


def test_quote_tolerates_whitespace_and_case():
    hit = locate_quote(
        "the ATTENTION mechanism in large FMs faces significant computational "
        "bottlenecks", PARAS)
    assert hit.ok and hit.para.no == 2


def test_elided_quote_falls_back_to_sentence():
    # 模型常把两处不相邻的话用「…」接起来，整串匹配必然失败，
    # 但其中保留完整的那一句仍然能定位。
    hit = locate_quote(
        "Pre-training for large FMs relies on a substantial amount of computation "
        "resources. The attention mechanism in large FMs faces significant "
        "computational bottlenecks primarily due to its quadratic complexity.",
        PARAS)
    assert hit.ok
    assert hit.how == "sentence"
    assert hit.para.no == 2


def test_fabricated_quote_is_rejected():
    hit = locate_quote(
        "Our proposed method achieves a 42% reduction in wall-clock time "
        "across all benchmark suites.", PARAS)
    assert not hit.ok
    assert hit.para is None


def test_rewritten_quote_is_rejected():
    # 意思对但换了词，不算「原文摘录」——这正是需要用户知道的那类问题
    hit = locate_quote(
        "Scissorhands utilizes an innovative compact KV cache and achieves a "
        "notable reduction in memory footprint.", PARAS)
    assert not hit.ok


def test_too_short_quote_is_skipped():
    assert not locate_quote("quadratic", PARAS).ok


def test_empty_input_is_safe():
    assert not locate_quote("", PARAS).ok
    assert not locate_quote("a long enough quote that goes nowhere at all", []).ok


# ---------------------------------------------------------------------------
# 批量标注
# ---------------------------------------------------------------------------

def test_annotate_fills_section_and_overrides_page():
    # 模型报 p.99，实际在第 10 页
    ev = Evidence(page=99, quote="draft-then-verify scheme, reporting a 2-3x "
                                 "improvement in decoding throughput")
    report = annotate([ev], PARAS)
    assert report.total == 1 and report.hits == 1
    assert ev.verified is True
    assert ev.page == 10          # 以定位结果为准
    assert ev.section_no == "4"
    assert ev.para == 3


def test_annotate_keeps_model_page_when_missed():
    ev = Evidence(page=7, quote="这句话在论文里根本不存在，是用来测试的假引文。")
    report = annotate([ev], PARAS)
    assert report.misses == 1
    assert ev.verified is False
    assert ev.page == 7           # 定位失败时不改动，保留模型给的线索


def test_annotate_skips_empty_quote():
    report = annotate([Evidence(page=1, quote="")], PARAS)
    assert report.total == 0


def test_report_rate():
    items = [
        Evidence(quote="draft-then-verify scheme, reporting a 2-3x improvement"),
        Evidence(quote="完全不存在的一句话，用来凑一个 miss 的计算。"),
    ]
    report = annotate(items, PARAS)
    assert report.total == 2 and report.hits == 1
    assert 0.4 < report.rate < 0.6
    assert "1/2" in report.summary()


# ---------------------------------------------------------------------------
# 坐标展示
# ---------------------------------------------------------------------------

def test_locator_full():
    ev = Evidence(section="4 RESOURCE-EFFICIENT ALGORITHMS", section_no="4",
                  para=3, page=11)
    assert ev.locator == "§4 · 第 3 段 · p.11"


def test_locator_without_number_uses_title():
    ev = Evidence(section="Abstract", para=1, page=1)
    assert ev.locator == "§ Abstract · 第 1 段 · p.1"


def test_locator_without_anything():
    assert Evidence().locator == "出处未标注"


def test_evidence_roundtrip_keeps_new_fields():
    ev = Evidence(page=11, quote="q", quote_zh="译文", section="4 X",
                  section_no="4", para=3, verified=True)
    again = Evidence.from_dict(ev.to_dict())
    assert (again.para, again.section_no, again.quote_zh) == (3, "4", "译文")
    assert again.verified is True


def test_evidence_reads_old_format():
    # 0.3 的 JSON 里只有 page 与 quote，必须还能读
    ev = Evidence.from_dict({"page": 3, "quote": "old style"})
    assert ev.page == 3 and ev.quote == "old style"
    assert ev.verified is None      # 未校验，而不是「校验失败」
    assert ev.para == 0


def test_evidence_accepts_plain_string():
    assert Evidence.from_dict("just a quote").quote == "just a quote"
