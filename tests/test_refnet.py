# -*- coding: utf-8 -*-
"""文献网络数据层：切条、标题提取、相关度与 payload 结构。

这批测试守的是本模块的立身之本：
1. 节点来源可追溯——payload 里每个节点都带 origin；
2. 标题提取宁缺毋滥——拿不准就返回空，绝不用作者名去撞库；
3. 网络不可用是降级而不是异常——报告的其余部分不能被它连坐。
"""

import pytest

from paperatomy.refnet import (
    OpenAlex,
    RefNode,
    _norm,
    build_network,
    extract_title,
    split_references,
)


# ---------------------------------------------------------------------------
# 切条
# ---------------------------------------------------------------------------

def test_split_numbered_references():
    text = ("[1] A. Author, First paper about task scheduling systems, 2020.\n"
            "[2] B. Author, Second paper about carbon aware computing, 2021.\n"
            "[3] C. Author, Third entry describing yet another method here, 2022.")
    entries = split_references(text)
    assert len(entries) == 3
    assert "First paper" in entries[0]
    assert "Third entry" in entries[2]


def test_split_ignores_short_fragments():
    text = "[1] A short one.\n[2] " + "A real entry with enough text to survive the filter. " * 2
    entries = split_references(text)
    assert len(entries) == 1
    assert "real entry" in entries[0]


def test_split_unnumbered_falls_back_to_paragraphs():
    text = "\n".join(
        "Author%d. A sufficiently long reference entry number %d, published somewhere." % (i, i)
        for i in range(6))
    entries = split_references(text)
    assert len(entries) == 6


# ---------------------------------------------------------------------------
# 标题提取：宁缺毋滥
# ---------------------------------------------------------------------------

def test_title_from_quotes():
    entry = 'X. Yang. "Deep learning for carbon-aware scheduling," In Proc. of ACM e-Energy, 2024.'
    assert extract_title(entry) == "Deep learning for carbon-aware scheduling"


def test_title_acm_lost_spaces_format():
    # A02 实拍：作者区丢空格、标题区正常、断行连字符断词
    entry = ("[1]BilgeAcun,BenjaminLee. Carbon Explorer: A Holis- tic Framework "
             "for Designing Carbon Aware Datacen- ters.In Proc. of the 28th "
             "ACM ASPLOS 2023, page 118–132, 2023.")
    t = extract_title(entry)
    assert "Carbon Explorer" in t
    assert "Holistic" in t          # 断词被合并
    assert "In Proc" not in t       # venue 段被跳过


def test_title_skips_venue_and_year_segments():
    entry = "Someone. A Genuine Method Title. In Proceedings of the 5th Workshop, 2019."
    assert extract_title(entry) == "A Genuine Method Title"


def test_title_returns_empty_when_unsure():
    # 全是作者与 venue，没有可靠标题——返回空而不是瞎猜
    entry = "BilgeAcun,BenjaminLee,FiodarKazhamiaka. https://kubernetes.io/docs, 2025."
    assert extract_title(entry) == ""


# ---------------------------------------------------------------------------
# 归一化与节点
# ---------------------------------------------------------------------------

def test_norm_keeps_meaningful_words():
    assert _norm("Signal-Aware DAG Scheduling!") == "signal aware dag scheduling"


def test_refnode_dict_roundtrip_shape():
    n = RefNode(title="T", kind="cited", origin="pdf", doi="10.1/x",
                cited_by=5, relevance=0.44)
    d = n.to_dict()
    assert d["origin"] == "pdf" and d["kind"] == "cited"
    assert d["relevance"] == 0.44 and d["doi"] == "10.1/x"


# ---------------------------------------------------------------------------
# build_network（mock OpenAlex——本测试不打网络）
# ---------------------------------------------------------------------------

class FakeOpenAlex(OpenAlex):
    """返回预设响应；记录每次请求的 URL 以便断言。"""

    def __init__(self, responses=None, **kw):
        super().__init__(**kw)
        self.responses = responses or []
        self.urls = []

    def _fetch(self, url):
        self.urls.append(url)
        for key, resp in self.responses:
            if key in url:
                return resp
        return None


@pytest.fixture
def fake_paper():
    """鸭子类型的 Paper：只需要 sections/paragraphs/title。"""

    class _S:
        is_reference = True
        text = ("[1] A. Author, Carbon-aware scheduling of data processing clusters "
                "for large scale systems. In ACM e-Energy, 2023.\n"
                "[2] B. Author, Unrelated_topic_from_nowhere with plenty of padding "
                "text to survive splitting, 2021.")

    class _P:
        title = "SPADE: Signal-Aware DAG Scheduling"
        sections = [_S()]
        paragraphs = []

    return _P()


def test_build_network_matches_and_annotates(fake_paper, monkeypatch):
    hit = {"results": [{
        "id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/abc",
        "display_name": "Carbon-aware scheduling of data processing clusters",
        "publication_year": 2023, "cited_by_count": 42,
        "referenced_works": [], "primary_location": {"source": {"display_name": "ACM e-Energy"}},
    }]}
    fa = FakeOpenAlex([("title.search", hit)])
    monkeypatch.setattr("paperatomy.refnet.OpenAlex", lambda **kw: fa)

    net = build_network(fake_paper)
    assert net["unavailable"] == ""
    assert net["stats"]["reference_entries"] == 2
    assert net["stats"]["matched"] == 1
    node = next(n for n in net["nodes"] if "Carbon-aware" in n["title"])
    assert node["doi"] == "10.1/abc"
    assert node["year"] == 2023 and node["cited_by"] == 42
    assert node["relevance"] > 0.2
    assert any("共享关键词" in r for r in node["reasons"])
    # 未命中的那条也保留——来源是 pdf
    other = next(n for n in net["nodes"] if "Unrelated" in n["title"])
    assert other["origin"] == "pdf"
    assert any("未在 OpenAlex 收录" in r for r in other["reasons"])


def test_build_network_degrades_when_api_down(fake_paper, monkeypatch):
    class Dead(FakeOpenAlex):
        def _fetch(self, url):
            self.ok = False     # 模拟真实客户端：请求失败必须留下痕迹
            return None

    monkeypatch.setattr("paperatomy.refnet.OpenAlex", lambda **kw: Dead(gap=0))
    net = build_network(fake_paper)
    # API 挂了：不抛异常，给出不可用说明，节点只剩 PDF 原文条目
    assert "无法访问" in net["unavailable"]
    assert all(n["origin"] == "pdf" for n in net["nodes"])


def test_center_unmatched_skips_citing(fake_paper, monkeypatch):
    fa = FakeOpenAlex([("title.search", {"results": []})])
    monkeypatch.setattr("paperatomy.refnet.OpenAlex", lambda **kw: fa)
    net = build_network(fake_paper)
    assert net["center"]["matched"] is False
    assert all(n["kind"] == "cited" for n in net["nodes"])
    assert net["stats"]["citing_available"] == 0
