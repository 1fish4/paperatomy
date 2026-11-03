# -*- coding: utf-8 -*-
"""compare 模块：对齐、模板差异提示、HTML 渲染。"""
import json

import pytest

from paperatomy.compare import compare_payload, run_compare, to_compare_html


def _report(tmp_path, name, template, block_ids, title="某论文"):
    findings = [{"block_id": b, "label": "板块" + b,
                 "content": "内容" + b,
                 "evidence": [{"page": 1, "quote": "q-" + b,
                               "quote_zh": "译-" + b,
                               "section_no": "1", "para": 2, "verified": True}]}
                for b in block_ids]
    p = tmp_path / name
    p.write_text(json.dumps({
        "paper_title": title, "template": template, "lang": "zh",
        "findings": findings,
        "chain": {"nodes": [{"type": "problem", "label": "问题"}]},
        "network": {"nodes": [1] * 5},
    }, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.fixture()
def pair(tmp_path):
    a = _report(tmp_path, "a.json", {"id": "survey"}, ["tl_dr", "problem", "gap"])
    b = _report(tmp_path, "b.json", {"id": "deep"}, ["tl_dr", "method"])
    return a, b


def test_rows_align_and_mark_exclusive(pair):
    a, b = pair
    payload = compare_payload(*map(lambda p: json.loads(p.read_text(encoding="utf-8")),
                                   pair))
    by_id = {r["block_id"]: r for r in payload["rows"]}
    assert by_id["tl_dr"]["only"] == ""          # 共有
    assert by_id["gap"]["only"] == "a"           # 仅 A
    assert by_id["method"]["only"] == "b"        # 仅 B
    assert payload["a"]["template"] == "survey"  # dict 模板被转成 id
    assert payload["a"]["chain"] == 1 and payload["b"]["network"] == 5


def test_html_marks_exclusives(pair):
    a, b = pair
    payload = compare_payload(json.loads(a.read_text(encoding="utf-8")),
                              json.loads(b.read_text(encoding="utf-8")))
    h = to_compare_html(payload)
    assert "仅A" in h and "仅B" in h
    assert "内容gap" in h and "内容method" in h
    assert "译-tl_dr" in h                       # 中英对照进页面
    assert "问题" in h                           # 论证链步骤
    assert "此报告无此板块" in h


def test_run_compare_writes_file(pair, tmp_path, capsys):
    a, b = pair
    rc = run_compare(str(a), str(b), str(tmp_path / "cmp"))
    assert rc == 0
    out = tmp_path / "cmp.compare.html"
    assert out.exists()
    assert "对比：某论文 vs 某论文" in out.read_text(encoding="utf-8")


def test_run_compare_missing_file(tmp_path):
    rc = run_compare(str(tmp_path / "no.json"),
                     str(tmp_path / "no2.json"))
    assert rc == 1
