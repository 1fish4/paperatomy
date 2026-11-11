# -*- coding: utf-8 -*-
"""动态讲解规范化测试：beats 节拍、新原语、实体回查与降级"""
from paperatomy.serve import _State

norm = _State._norm_demo

BLOB = ("任务映射到机器 greenheft 只优化能耗 截止时间 "
        "工作流 贪心 局部搜索 通信开销").lower()


def test_beats_kept_and_narration_joined():
    sc = {"title": "t", "scenes": [{
        "viz": {"type": "tokens", "caption": "c", "groups": [
            {"label": "a", "color": "red", "count": 3},
            {"label": "b", "color": "teal", "count": 3}]},
        "beats": [
            {"say": "第一拍", "act": {"kind": "reveal", "target": 0}},
            {"say": "第二拍", "act": {"kind": "reveal", "target": 1}},
            {"say": "第三拍", "act": {"kind": "highlight", "target": 1}},
        ]}]}
    out = norm(sc)
    s = out["scenes"][0]
    assert len(s["beats"]) == 3
    assert s["beats"][2]["act"] == {"kind": "highlight", "target": 1}
    assert "第一拍" in s["narration"] and "第三拍" in s["narration"]


def test_move_act_validation():
    sc = {"scenes": [{
        "narration": "n",
        "viz": {"type": "tokens", "groups": [
            {"label": "a", "color": "red", "count": 2},
            {"label": "b", "color": "teal", "count": 2}]},
        "beats": [
            {"say": "s1", "act": {"kind": "move", "from": 0, "to": 1, "n": 2}},
            {"say": "s2", "act": {"kind": "move", "from": 1, "to": 1}},
            {"say": "s3", "act": {"kind": "move", "from": 0, "to": 9}},
        ]}]}
    bts = norm(sc)["scenes"][0]["beats"]
    assert len(bts) == 1  # from==to 与越界的都被丢
    assert bts[0]["act"] == {"kind": "move", "from": 0, "to": 1, "n": 2}


def test_mapping_entity_check():
    sc = {"scenes": [{
        "narration": "n",
        "viz": {"type": "mapping", "caption": "c",
                "left": ["工作流", "编造实体", "贪心"],
                "right": ["机器", "GREENHEFT"],
                "links": [[0, 0], [1, 1], [2, 0]]}}]}
    s = norm(sc, BLOB)["scenes"][0]
    v = s["viz"]
    assert v["type"] == "mapping"
    assert v["left"] == ["工作流", "贪心"]
    assert "编造实体" not in v["left"]
    # 过滤后 left=[工作流,贪心]，links 全部落在合法索引内
    assert v["links"] == [[0, 0], [1, 1]]


def test_mapping_all_bad_dropped():
    sc = {"scenes": [{
        "narration": "n",
        "viz": {"type": "mapping", "left": ["编1", "编2"],
                "right": ["编3", "编4"], "links": [[0, 1]]}}]}
    s = norm(sc, BLOB)["scenes"][0]
    assert "type" not in s["viz"]  # 画面被丢，纯旁白


def test_transform_check():
    sc = {"scenes": [{
        "narration": "n",
        "viz": {"type": "transform", "caption": "c",
                "before": {"blocks": ["全部对全部", "softmax", "胡说的"]},
                "after": {"blocks": ["稀疏近似", "线性递推"]},
                "note": "结构替换"}}]}
    s = norm(sc, "全部对全部 softmax 稀疏近似 线性递推".lower())["scenes"][0]
    v = s["viz"]
    assert v["type"] == "transform"
    assert "胡说的" not in v["before"]["blocks"]
    assert v["before"]["blocks"] == ["全部对全部", "softmax"]


def test_pipeline_needs_three_stages():
    sc = {"scenes": [{
        "narration": "n",
        "viz": {"type": "pipeline", "stages": [
            {"name": "解析", "note": "a"}, {"name": "抽取", "note": "b"}]}}]}
    s = norm(sc, "解析 抽取")["scenes"][0]
    assert "type" not in s["viz"]


def test_pipeline_ok():
    sc = {"scenes": [{
        "narration": "n",
        "viz": {"type": "pipeline", "stages": [
            {"name": "解析", "note": "a"}, {"name": "抽取", "note": "b"},
            {"name": "定位", "note": "c"}]}}]}
    s = norm(sc, "解析 抽取 定位")["scenes"][0]
    assert s["viz"]["type"] == "pipeline"
    assert len(s["viz"]["stages"]) == 3


def test_old_style_scene_unchanged():
    sc = {"scenes": [{
        "narration": "旧式镜没有 beats",
        "viz": {"type": "flow", "steps": ["步骤一", "步骤二", "步骤三"]}}]}
    s = norm(sc)["scenes"][0]
    assert "beats" not in s
    assert s["viz"]["type"] == "flow"


def test_bad_beats_fallback_to_narration():
    sc = {"scenes": [{
        "viz": {"type": "flow", "steps": ["a", "b"]},
        "beats": [{"say": "", "act": None}]}]}
    # beats 全被丢 → 场景被丢（没有 narration 也没有有效 beats）
    assert norm(sc)["scenes"] == []
