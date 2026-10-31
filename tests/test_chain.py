"""论证链的测试。

除了测代码行为，这里守住三条设计约束：

1. **空链是合法结果** —— 抽不出链条不是失败，硬编一条才是
2. **改动必须给前后对照** —— 没有 before/after 的 change 就退化成一句空话
3. **关键节点必须有出处** —— change 与 effect 没有 evidence 一律拒绝
"""

from __future__ import annotations

import json

import pytest

from paperatomy.chain import (
    MAX_NODES,
    MIN_NODES,
    NODE_TYPES,
    Chain,
    ChainNode,
    _num,
    build_chain_messages,
    chain_warnings,
    list_node_types,
    parse_chain_response,
    validate_chain,
)
from paperatomy.model import Block, Evidence, Extraction, Finding, Template
from paperatomy.parse import Page, Paper
from paperatomy.render import chain_from_json, to_html, to_json, to_markdown


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------

def _ev(page: int = 3, quote: str = "quote") -> Evidence:
    return Evidence(page=page, quote=quote)


def _node(node_id: str, node_type: str, label: str = "某环节", **kw) -> ChainNode:
    return ChainNode(
        id=node_id, type=node_type, label=label,
        detail=kw.pop("detail", "说明文字"),
        evidence=kw.pop("evidence", [_ev()]),
        **kw,
    )


def _change(**kw) -> ChainNode:
    kw.setdefault("before", {"kind": "tokens", "n": 8, "cadence": 1.4})
    kw.setdefault("after", {"kind": "tokens", "n": 8, "cadence": 0.45})
    return _node("n3", "change", "串行改为先猜后验", **kw)


def _valid_chain() -> Chain:
    return Chain(
        thesis="先猜后验把串行解码压成并行",
        confidence="high",
        nodes=[
            _node("n1", "problem", "逐 token 太慢",
                  metric={"value": "T", "unit": "次", "label": "串行步数"}),
            _node("n2", "insight", "便宜模型先猜"),
            _change(),
            _node("n4", "mechanism", "一次验证多个 token"),
            _node("n5", "effect", "解码提速",
                  metric={"value": "2.6", "unit": "×", "label": "吞吐"}),
            _node("n6", "cost", "需要额外草稿模型"),
        ],
    )


def _paper() -> Paper:
    return Paper(title="A Test Paper", pages=[Page(number=1, text="Body. " * 60)])


def _template() -> Template:
    return Template(
        id="t", name="测试模板",
        blocks=[Block(id="tl_dr", title="一句话结论", group="概览", hint="一句话")],
    )


def _extraction() -> Extraction:
    return Extraction(
        template_id="t", paper_title="测试论文", lang="zh", model="test",
        findings=[Finding(block_id="tl_dr", content="他们提出了 X",
                          evidence=[_ev(1, "We propose X")])],
    )


# ---------------------------------------------------------------------------
# 节点类型目录
# ---------------------------------------------------------------------------

def test_node_types_cover_the_argument():
    names = [t.name for t in list_node_types()]
    for must in ("problem", "insight", "change", "mechanism", "effect", "cost"):
        assert must in names


def test_node_types_have_label_and_color():
    for spec in list_node_types():
        assert spec.label.strip()
        assert spec.color.startswith("#")
        assert spec.guidance.strip()


def test_only_change_needs_before_after_and_only_effect_needs_metric():
    flags_before = [t.name for t in list_node_types() if t.needs_before_after]
    flags_metric = [t.name for t in list_node_types() if t.needs_metric]
    assert flags_before == ["change"]
    assert flags_metric == ["effect"]


# ---------------------------------------------------------------------------
# 校验：整体结构
# ---------------------------------------------------------------------------

def test_valid_chain_passes():
    assert validate_chain(_valid_chain()) == []


def test_empty_chain_is_legal():
    """抽不出链条是合法结果，不该触发修复循环逼模型去编。"""
    assert validate_chain(Chain()) == []


def test_too_few_nodes_rejected():
    chain = Chain(nodes=[_node("n1", "problem"), _change()])
    assert len(chain.nodes) < MIN_NODES
    assert any("应在" in i for i in validate_chain(chain))


def test_too_many_nodes_rejected():
    nodes = [_node(f"n{i}", "mechanism") for i in range(MAX_NODES + 1)]
    nodes[0] = _node("n0", "problem")
    assert any("应在" in i for i in validate_chain(Chain(nodes=nodes)))


def test_chain_must_start_from_problem():
    chain = Chain(nodes=[_change(), _node("n4", "effect", "效果",
                                          metric={"value": "2"})])
    assert any("problem" in i for i in validate_chain(chain))


def test_chain_needs_change_or_taxonomy():
    chain = Chain(nodes=[
        _node("n1", "problem"),
        _node("n2", "insight"),
        _node("n3", "mechanism"),
    ])
    assert any("change 或 taxonomy" in i for i in validate_chain(chain))


def test_duplicate_id_rejected():
    chain = _valid_chain()
    chain.nodes[1].id = "n1"
    assert any("重复" in i for i in validate_chain(chain))


def test_label_too_long_rejected():
    chain = _valid_chain()
    chain.nodes[0].label = "一" * 20
    assert any("最多 16 字" in i for i in validate_chain(chain))


def test_unknown_type_rejected():
    chain = _valid_chain()
    chain.nodes[1].type = "hallucination"
    assert any("不在" in i for i in validate_chain(chain))


def test_chain_needs_some_evidence():
    chain = _valid_chain()
    for n in chain.nodes:
        n.evidence = []
    chain.evidence = []
    assert any("原文依据" in i for i in validate_chain(chain))


# ---------------------------------------------------------------------------
# 校验：change 是整条链的眼
# ---------------------------------------------------------------------------

def test_change_requires_both_sides():
    node = _node("n3", "change", "改了点东西")
    assert any("before 与 after" in i for i in validate_chain(
        Chain(nodes=[_node("n1", "problem"), node, _node("n2", "mechanism")])
    ))


def test_change_requires_evidence():
    node = _change(evidence=[])
    chain = Chain(nodes=[_node("n1", "problem"), node, _node("n2", "mechanism")])
    assert any("change 节点 n3 没有给出原文依据" in i for i in validate_chain(chain))


def test_change_diagram_kind_must_be_known():
    node = _change(before={"kind": "hologram", "n": 3})
    chain = Chain(nodes=[_node("n1", "problem"), node, _node("n2", "mechanism")])
    assert any("图示 kind" in i for i in validate_chain(chain))


@pytest.mark.parametrize("spec,needle", [
    ({"kind": "tokens", "n": 0}, "tokens.n"),
    ({"kind": "tokens", "n": 20}, "tokens.n"),
    ({"kind": "tokens", "n": 4, "cadence": 9}, "cadence"),
    ({"kind": "blocks", "items": []}, "blocks.items"),
    ({"kind": "blocks", "items": ["a", ""]}, "空项"),
    ({"kind": "bars", "items": [{"label": "x"}]}, "缺少数值"),
    ({"kind": "text", "text": "  "}, "text 为空"),
])
def test_diagram_spec_validation(spec, needle):
    node = _change(before=spec)
    chain = Chain(nodes=[_node("n1", "problem"), node, _node("n2", "mechanism")])
    assert any(needle in i for i in validate_chain(chain))


# ---------------------------------------------------------------------------
# 校验：effect 必须有数字
# ---------------------------------------------------------------------------

def test_effect_requires_numeric_metric():
    node = _node("n5", "effect", "效果很好", metric={"value": "显著提升"})
    chain = Chain(nodes=[_node("n1", "problem"), _change(), node])
    assert any("可读的数字" in i for i in validate_chain(chain))


def test_effect_requires_evidence():
    node = _node("n5", "effect", "提速 2 倍",
                 metric={"value": "2"}, evidence=[])
    chain = Chain(nodes=[_node("n1", "problem"), _change(), node])
    assert any("没有给出原文依据" in i for i in validate_chain(chain))


# ---------------------------------------------------------------------------
# 校验：taxonomy
# ---------------------------------------------------------------------------

def test_taxonomy_depth_limit():
    root = {"name": "L0", "children": [
        {"name": "L1", "children": [{"name": "L2", "children": [{"name": "L3"}]}]}
    ]}
    node = _node("n3", "taxonomy", "框架", root=root)
    chain = Chain(nodes=[_node("n1", "problem"), node, _node("n2", "insight")])
    assert any("最多 3 层" in i for i in validate_chain(chain))


def test_taxonomy_ok():
    root = {"name": "总", "children": [{"name": "甲"}, {"name": "乙"}]}
    node = _node("n3", "taxonomy", "框架", root=root)
    chain = Chain(nodes=[_node("n1", "problem"), node, _node("n2", "insight")])
    assert validate_chain(chain) == []


# ---------------------------------------------------------------------------
# 警告（不阻止渲染）
# ---------------------------------------------------------------------------

def test_warn_when_mechanism_missing():
    chain = Chain(nodes=[_node("n1", "problem"), _change()])
    assert any("机制" in w for w in chain_warnings(chain))


def test_warn_when_cost_missing():
    # _valid_chain() 已经带了 cost，所以这里必须造一条没有 cost 的
    chain = Chain(nodes=[
        _node("n1", "problem"), _change(), _node("n4", "mechanism"),
        _node("n5", "effect", "提速", metric={"value": "2"}),
    ])
    assert any("代价" in w for w in chain_warnings(chain))


def test_no_warnings_for_empty_chain():
    assert chain_warnings(Chain()) == []


def test_warn_when_change_falls_back_to_text():
    node = _change(before={"kind": "text", "text": "只能文字描述"})
    chain = Chain(nodes=[_node("n1", "problem"), node, _node("n6", "cost")])
    assert any("无法图示" in w for w in chain_warnings(chain))


# ---------------------------------------------------------------------------
# 解析模型返回
# ---------------------------------------------------------------------------

def test_parse_plain_json():
    raw = json.dumps(_valid_chain().to_dict(), ensure_ascii=False)
    chain = parse_chain_response(raw)
    assert chain is not None
    assert len(chain.nodes) == 6
    assert chain.nodes[2].type == "change"


def test_parse_fenced_json():
    raw = "```json\n" + json.dumps({"nodes": []}) + "\n```"
    chain = parse_chain_response(raw)
    assert chain is not None and chain.nodes == []


def test_parse_garbage_returns_none():
    """解析不了要能和「模型明确说没有链」区分开。"""
    assert parse_chain_response("这篇论文我演不出来。") is None


def test_parse_empty_nodes_is_not_none():
    chain = parse_chain_response('{"nodes": [], "note": "纯理论论文"}')
    assert chain is not None and chain.nodes == []


def test_roundtrip_dict():
    origin = _valid_chain()
    back = Chain.from_dict(origin.to_dict())
    assert back.thesis == origin.thesis
    assert [n.type for n in back.nodes] == [n.type for n in origin.nodes]
    assert back.nodes[2].before["kind"] == "tokens"


def test_num_parses_ranges_and_units():
    assert _num("2.6") == 2.6
    assert _num("2–3×") == 2.0
    assert _num("约 15%") == 15.0
    assert _num("显著提升") is None


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------

def test_messages_include_types_and_material():
    msgs = build_chain_messages(_paper())
    system = msgs[0]["content"]
    for name in NODE_TYPES:
        assert name in system
    assert "不要编" in system or "编" in system
    assert "Body." in msgs[1]["content"]


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def test_html_embeds_chain():
    html = to_html(_extraction(), _template(), chain=_valid_chain())
    assert '"chain"' in html
    assert "先猜后验把串行解码压成并行" in html
    assert "串行改为先猜后验" in html


def test_html_without_chain_is_null():
    html = to_html(_extraction(), _template())
    assert '"chain":null' in html.replace(" ", "")


def test_json_roundtrip_chain():
    raw = to_json(_extraction(), _template(), chain=_valid_chain())
    data = json.loads(raw)
    assert data["chain"]["nodes"][2]["type"] == "change"
    back = chain_from_json(data)
    assert len(back.nodes) == 6


def test_json_keeps_warnings():
    # 一条缺「机制」的链，警告应当随 JSON 一起下发到前端
    chain = Chain(
        thesis="t",
        nodes=[
            _node("n1", "problem"), _change(),
            _node("n4", "effect", "提速", metric={"value": "2"}),
        ],
    )
    data = json.loads(to_json(_extraction(), _template(), chain=chain))
    assert data["chain"]["warnings"]


def test_json_downloads_type_metadata():
    data = json.loads(to_json(_extraction(), _template(), chain=_valid_chain()))
    assert data["chain"]["types"]["change"]["label"] == "改动"
    assert data["chain"]["types"]["change"]["color"].startswith("#")


def test_markdown_lists_chain():
    md = to_markdown(_extraction(), _template(), chain=_valid_chain())
    assert "## 论证链" in md
    assert "串行改为先猜后验" in md
    assert "改动前" in md and "改动后" in md
    assert "2.6" in md


def test_markdown_without_chain():
    md = to_markdown(_extraction(), _template())
    assert "## 论证链" not in md
