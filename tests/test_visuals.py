"""可视化原语的测试。

这里除了测代码行为，还守住三条设计约束：

1. **没有证据不许渲染** —— 原则二：宁可不演，不编数据
2. **example 必须有卡点** —— 没有卡点的「例子」等于复述流程
3. **一个原语只演一件事** —— 同一原语重复出现要报错
"""

from __future__ import annotations

import json

import pytest

from paperatomy.model import (
    Evidence,
    Extraction,
    Finding,
    Template,
    Block,
)
from paperatomy.render import to_html, to_json, to_markdown, visuals_from_json
from paperatomy.parse import Page, Paper
from paperatomy.visuals import (
    MAX_VISUALS,
    PRIMITIVES,
    Visual,
    build_visual_messages,
    list_primitives,
    parse_visual_response,
    validate_visual,
    validate_visuals,
    visual_warnings,
)


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------

def _pipeline(**over) -> Visual:
    kwargs = {
        "primitive": "pipeline",
        "title": "两阶段检索",
        "params": {
            "stages": [
                {"name": "粗排", "what": "从百万候选筛出 1000 条", "why": "全量精排成本过高"},
                {"name": "精排", "what": "对 1000 条逐条打分", "why": "候选少，可用重模型"},
            ]
        },
        "evidence": [Evidence(page=4, quote="We adopt a two-stage pipeline.")],
    }
    if "params" in over:
        kwargs["params"] = {**kwargs["params"], **over.pop("params")}
    kwargs.update(over)
    return Visual(**kwargs)


def _example(**over) -> Visual:
    params = {
        "scenario": "新用户刚注册，没有任何历史行为",
        "steps": [
            {"state": "候选池有 100 万篇文章", "issue": "不知道该推哪一篇"},
            {"state": "按热度推荐", "issue": None},
        ],
    }
    params.update(over.pop("params", {}))
    return Visual(
        primitive="example",
        title="冷启动为什么难",
        params=params,
        evidence=[Evidence(page=2, quote="Cold-start users have no history.")],
        **over,
    )


def _taxonomy(**over) -> Visual:
    params = {
        "root": {
            "name": "高效基础模型",
            "children": [
                {"name": "架构", "what": "改结构", "children": [{"name": "Mamba"}]},
                {"name": "算法", "what": "改训练", "children": [{"name": "QLoRA"}]},
            ],
        }
    }
    params.update(over.pop("params", {}))
    return Visual(
        primitive="taxonomy",
        title="三层分类框架",
        params=params,
        evidence=[Evidence(page=3, quote="We organize existing work into three layers.")],
        **over,
    )


def _paper() -> Paper:
    return Paper(
        title="A Test Paper",
        pages=[Page(number=1, text="Body text. " * 50)],
    )


def _template() -> Template:
    return Template(
        id="t", name="测试模板",
        blocks=[Block(id="tl_dr", title="一句话结论", group="概览", hint="一句话")],
    )


# ---------------------------------------------------------------------------
# 原语目录
# ---------------------------------------------------------------------------

def test_builtin_primitives():
    names = [s.name for s in list_primitives()]
    assert names == ["pipeline", "example", "taxonomy"]


def test_primitive_catalog_has_params_doc():
    """每个原语都要有参数说明，否则模型不知道该怎么填。"""
    for spec in list_primitives():
        assert spec.params_doc.strip()
        assert spec.purpose.strip()


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

def test_pipeline_ok():
    assert validate_visual(_pipeline()) == []


def test_pipeline_needs_two_stages():
    v = _pipeline(params={"stages": [{"name": "仅一步", "what": "做了点事"}]})
    issues = validate_visual(v)
    assert any("至少需要 2" in i for i in issues)


def test_pipeline_requires_name_and_what():
    v = _pipeline(params={"stages": [{"name": "", "what": ""}, {"name": "b", "what": "c"}]})
    issues = validate_visual(v)
    assert any("缺少 name" in i for i in issues)
    assert any("缺少 what" in i for i in issues)


def test_pipeline_allows_missing_why_but_warns():
    """没有 why 不算错，但要提醒——这往往说明论文没讲清必要性。"""
    v = _pipeline(params={"stages": [
        {"name": "A", "what": "x", "why": ""},
        {"name": "B", "what": "y", "why": "z"},
    ]})
    assert validate_visual(v) == []
    warns = visual_warnings(v)
    assert any("A" in w for w in warns)


# ---------------------------------------------------------------------------
# example
# ---------------------------------------------------------------------------

def test_example_ok():
    assert validate_visual(_example()) == []


def test_example_without_issue_is_rejected():
    """没有卡点的例子等于复述流程，这个原语就没有存在价值。"""
    v = _example(params={"steps": [
        {"state": "第一步", "issue": None},
        {"state": "第二步", "issue": None},
    ]})
    issues = validate_visual(v)
    assert any("没有卡点" in i for i in issues)


def test_example_needs_scenario():
    v = _example(params={"scenario": ""})
    assert any("scenario" in i for i in validate_visual(v))


# ---------------------------------------------------------------------------
# taxonomy
# ---------------------------------------------------------------------------

def test_taxonomy_ok():
    assert validate_visual(_taxonomy()) == []


def test_taxonomy_depth_limit():
    deep = {"name": "L0", "children": [
        {"name": "L1", "children": [
            {"name": "L2", "children": [{"name": "L3", "children": []}]}
        ]}
    ]}
    v = _taxonomy(params={"root": deep})
    assert any("最多 3 层" in i for i in validate_visual(v))


def test_taxonomy_needs_enough_nodes():
    v = _taxonomy(params={"root": {"name": "唯一节点"}})
    assert any("至少需要 3" in i for i in validate_visual(v))


# ---------------------------------------------------------------------------
# 通用约束
# ---------------------------------------------------------------------------

def test_unknown_primitive_rejected():
    v = Visual(primitive="hologram", title="x", params={"a": 1},
               evidence=[Evidence(page=1, quote="q")])
    assert any("未知原语" in i for i in validate_visual(v))


def test_visual_without_evidence_is_rejected():
    """原则二：没有原文依据，宁可不演。"""
    v = _pipeline(evidence=[])
    assert any("原文依据" in i for i in validate_visual(v))


def test_title_required():
    v = _pipeline(title="")
    assert any("title" in i for i in validate_visual(v))


def test_duplicate_primitive_rejected():
    issues = validate_visuals([_pipeline(), _pipeline()])
    assert any("重复" in i for i in issues)


def test_too_many_visuals_rejected():
    vs = [_pipeline(), _example(), _taxonomy()]
    extra = Visual(primitive="pipeline", title="多余", params={},
                   evidence=[Evidence(page=1, quote="q")])
    issues = validate_visuals(vs + [extra])
    assert any(f"最多 {MAX_VISUALS}" in i for i in issues)


# ---------------------------------------------------------------------------
# 解析模型返回
# ---------------------------------------------------------------------------

def test_parse_plain_json():
    raw = json.dumps({"visuals": [
        {"primitive": "pipeline", "title": "T",
         "params": {"stages": [{"name": "a", "what": "b"}, {"name": "c", "what": "d"}]},
         "evidence": [{"page": 1, "quote": "q"}], "confidence": "high"}
    ]}, ensure_ascii=False)
    vs = parse_visual_response(raw)
    assert len(vs) == 1
    assert vs[0].primitive == "pipeline"
    assert vs[0].evidence[0].page == 1


def test_parse_fenced_json():
    raw = '```json\n{"visuals": []}\n```'
    assert parse_visual_response(raw) == []


def test_parse_garbage_returns_empty():
    assert parse_visual_response("我不知道该怎么演。") == []


def test_empty_visuals_is_legal():
    """演不出来是合法结果——不能被当成失败。"""
    assert validate_visuals([]) == []


def test_roundtrip_dict():
    v = _pipeline()
    again = Visual.from_dict(v.to_dict())
    assert again.primitive == v.primitive
    assert again.params["stages"][0]["name"] == "粗排"
    assert again.evidence[0].page == 4


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------

def test_messages_include_catalog_and_material():
    msgs = build_visual_messages(_paper())
    system = msgs[0]["content"]
    for name in PRIMITIVES:
        assert name in system
    assert "不要为了凑数而编造" in system
    assert "A Test Paper" not in system  # 标题在正文里，不在 system
    assert "Body text" in msgs[1]["content"]


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _extraction() -> Extraction:
    return Extraction(
        template_id="t", paper_title="测试论文", lang="zh", model="test",
        findings=[Finding(block_id="tl_dr", content="他们提出了 X",
                          evidence=[Evidence(page=1, quote="We propose X")])],
    )


def test_html_embeds_visuals():
    html = to_html(_extraction(), _template(), visuals=[_pipeline()])
    assert '"visuals"' in html
    assert "两阶段检索" in html
    assert "粗排" in html


def test_html_without_visuals_has_empty_array():
    html = to_html(_extraction(), _template())
    assert '"visuals":[]' in html.replace(" ", "")


def test_json_roundtrip_visuals():
    raw = to_json(_extraction(), _template(), visuals=[_pipeline(), _example()])
    data = json.loads(raw)
    assert len(data["visuals"]) == 2
    back = visuals_from_json(data)
    assert [v.primitive for v in back] == ["pipeline", "example"]


def test_json_includes_warnings():
    v = _pipeline(params={"stages": [
        {"name": "A", "what": "x", "why": ""},
        {"name": "B", "what": "y", "why": "z"},
    ]})
    data = json.loads(to_json(_extraction(), _template(), visuals=[v]))
    assert data["visuals"][0]["warnings"]


def test_markdown_lists_visual_structure():
    md = to_markdown(_extraction(), _template(), visuals=[_pipeline()])
    assert "## 动态演示" in md
    assert "粗排" in md
    assert "为什么必须有：全量精排成本过高" in md


def test_markdown_example_marks_issue():
    md = to_markdown(_extraction(), _template(), visuals=[_example()])
    assert "卡点" in md
