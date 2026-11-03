# -*- coding: utf-8 -*-
"""review 模块测试。"""
import json

from paperatomy.review import review_markdown, run_review


def _report(tmp_path, name, title, terms, blocks=("tl_dr", "problem")):
    findings = [{"block_id": b, "label": "板块" + b, "content": "内容" + b,
                 "evidence": [{"page": 2, "quote": "qq", "quote_zh": "译",
                               "section_no": "1", "para": 3, "verified": True}]}
                for b in blocks]
    p = tmp_path / name
    p.write_text(json.dumps({
        "paper_title": title, "template": {"id": "survey"},
        "findings": findings,
        "chain": {"nodes": [{"type": "problem", "label": "瓶颈"}]},
        "network": {"center": {"venue": "ACM CSUR", "ccf": "",
                               "search_terms": terms, "topics": []}, "nodes": []},
    }, ensure_ascii=False), encoding="utf-8")
    return p


def test_review_merges_and_ranks_terms(tmp_path):
    a = _report(tmp_path, "a.json", "论文A", ["dag scheduling", "carbon"])
    b = _report(tmp_path, "b.json", "论文B", ["dag scheduling", "heft"])
    md = review_markdown(
        [json.loads(a.read_text(encoding="utf-8")),
         json.loads(b.read_text(encoding="utf-8"))], ["a", "b"])
    assert "dag scheduling**（2 篇）" in md      # 共现关键词排前且带计数
    assert "论文A" in md and "论文B" in md
    assert "ACM CSUR" in md
    assert "problem·瓶颈" in md                   # 论证链一行摘要
    assert "译" in md                              # 证据（优先中文）进草稿
    assert "不是成稿" in md                        # 诚实定位声明


def test_review_without_terms_still_works(tmp_path):
    a = _report(tmp_path, "a.json", "论文A", [])
    b = _report(tmp_path, "b.json", "论文B", [])
    md = review_markdown(
        [json.loads(a.read_text(encoding="utf-8")),
         json.loads(b.read_text(encoding="utf-8"))], ["a", "b"])
    assert "## 1. 论文A" in md


def test_run_review_writes(tmp_path):
    a = _report(tmp_path, "a.json", "论文A", ["x"])
    b = _report(tmp_path, "b.json", "论文B", ["x"])
    rc = run_review([str(a), str(b)], str(tmp_path / "rev.md"))
    assert rc == 0
    assert (tmp_path / "rev.md").exists()


def test_run_review_needs_two(tmp_path):
    a = _report(tmp_path, "a.json", "论文A", ["x"])
    assert run_review([str(a)]) == 1
    assert run_review([str(tmp_path / "no.json"), str(a)]) == 1
