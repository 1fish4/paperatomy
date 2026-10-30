"""模板加载、校验与语言回落的测试。

这里除了测行为，还守住**模板质量**：每个板块必须有 hint。
没有 hint 的板块等于让模型自由发挥，输出必然跑偏。
"""

from __future__ import annotations

import pytest
import yaml

from paperatomy.model import (
    Evidence,
    Extraction,
    Finding,
    list_templates,
    load_template,
    validate_extraction,
)


# ---------------------------------------------------------------------------
# 内置模板
# ---------------------------------------------------------------------------

def test_builtin_templates_load():
    templates = list_templates()
    assert len(templates) >= 2
    ids = {t.id for t in templates}
    assert {"skim", "deep"} <= ids


def test_template_ids_are_unique():
    ids = [t.id for t in list_templates()]
    assert len(ids) == len(set(ids))


def test_skim_has_six_blocks():
    t = load_template(next(p for p in _template_paths() if p.name == "skim.yaml"))
    assert len(t.blocks) == 6
    assert len(t.required_ids) == 6


def test_deep_has_more_blocks_than_skim():
    by_id = {t.id: t for t in list_templates()}
    assert len(by_id["deep"].blocks) > len(by_id["skim"].blocks)


def test_every_block_has_hint():
    """每个板块都必须写抽取指令——没有 hint 的板块会让模型自由发挥。"""
    missing = [
        f"{t.id}.{b.id}"
        for t in list_templates()
        for b in t.blocks
        if not b.hint.strip()
    ]
    assert not missing, f"这些板块没有 hint：{missing}"


def test_every_block_has_title_and_id():
    for t in list_templates():
        for b in t.blocks:
            assert b.id.strip()
            assert b.title.strip()


def test_groups_are_ordered_and_unique():
    for t in list_templates():
        groups = t.groups()
        assert len(groups) == len(set(groups))


# ---------------------------------------------------------------------------
# 语言回落
# ---------------------------------------------------------------------------

def test_title_falls_back_to_chinese_when_english_missing():
    t = list_templates()[0]
    block = t.blocks[0]
    assert block.title_in("zh") == block.title
    if not block.title_en:
        assert block.title_in("en") == block.title


def test_english_title_used_when_present():
    t = next(x for x in list_templates() if x.id == "skim")
    block = next(b for b in t.blocks if b.title_en)
    assert block.title_in("en") == block.title_en
    assert block.title_in("zh") == block.title


# ---------------------------------------------------------------------------
# 加载期的错误拦截
# ---------------------------------------------------------------------------

def _write(tmp_path, data):
    p = tmp_path / "t.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


def _minimal(**over):
    data = {
        "id": "x",
        "name": "测试",
        "blocks": [{"id": "a", "title": "板块A", "hint": "写点什么"}],
    }
    data.update(over)
    return data


def test_missing_id_rejected(tmp_path):
    p = _write(tmp_path, {"name": "无 id", "blocks": [{"id": "a", "title": "A"}]})
    with pytest.raises(ValueError, match="id"):
        load_template(p)


def test_missing_blocks_rejected(tmp_path):
    with pytest.raises(ValueError, match="blocks"):
        load_template(_write(tmp_path, _minimal(blocks=[])))


def test_duplicate_block_id_rejected(tmp_path):
    blocks = [
        {"id": "a", "title": "A"},
        {"id": "a", "title": "重复"},
    ]
    with pytest.raises(ValueError, match="重复"):
        load_template(_write(tmp_path, _minimal(blocks=blocks)))


def test_invalid_evidence_mode_rejected(tmp_path):
    blocks = [{"id": "a", "title": "A", "evidence": "sometimes"}]
    with pytest.raises(ValueError, match="evidence"):
        load_template(_write(tmp_path, _minimal(blocks=blocks)))


# ---------------------------------------------------------------------------
# 抽取结果校验
# ---------------------------------------------------------------------------

def _skim():
    return next(t for t in list_templates() if t.id == "skim")


def test_validate_reports_missing_blocks():
    ex = Extraction(template_id="skim", findings=[])
    issues = validate_extraction(ex, _skim())
    assert any("缺少必填板块" in i for i in issues)


def test_validate_reports_unknown_block():
    ex = Extraction(template_id="skim", findings=[
        Finding("tl_dr", "结论"),
        Finding("ghost", "模板里没有这块"),
    ])
    issues = validate_extraction(ex, _skim())
    assert any("模板未定义" in i for i in issues)


def test_validate_reports_missing_evidence():
    t = load_template(next(p for p in _template_paths() if p.name == "skim.yaml"))
    ex = Extraction(template_id="skim", findings=[
        Finding("problem", "问题描述", evidence=[]),
    ])
    issues = validate_extraction(ex, t)
    assert any("原文证据" in i for i in issues)


def test_validate_passes_on_complete_extraction():
    t = _skim()
    findings = [
        Finding(b.id, f"{b.title}的内容", evidence=[Evidence(1, "原文")])
        for b in t.blocks
    ]
    ex = Extraction(template_id=t.id, findings=findings)
    assert validate_extraction(ex, t) == []


# ---------------------------------------------------------------------------
# 抽取结果对象的语义
# ---------------------------------------------------------------------------

def test_empty_marker_counts_as_empty():
    assert Finding("a", "论文未提及").is_empty
    assert Finding("a", "not reported").is_empty
    assert not Finding("a", "有内容").is_empty


def test_low_confidence_needs_review():
    assert Finding("a", "内容", confidence="low").needs_review
    assert Finding("a", "内容", evidence=[], confidence="high").needs_review


def _template_paths():
    from paperatomy.model import TEMPLATE_DIR
    return sorted(TEMPLATE_DIR.glob("*.yaml"))
