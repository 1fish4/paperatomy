# -*- coding: utf-8 -*-
"""追问历史持久化：qa 追加进报告 JSON。"""
import json

import pytest


def _handler(state, workbench=True, single_json=None):
    """造一个只挂 _save_qa 所需属性的最小 handler。"""
    from paperatomy.serve import _Handler
    h = object.__new__(_Handler)
    h.workbench = workbench
    h.state = state if workbench else None
    h.single_json = single_json
    return h


def _write_report(out_dir, name, **extra):
    data = {"paper_title": "某论文", "findings": []}
    data.update(extra)
    (out_dir / name).write_text(json.dumps(data, ensure_ascii=False),
                                encoding="utf-8")
    return data


def test_save_qa_appends_workbench(tmp_path):
    from paperatomy.serve import _State
    state = _State(tmp_path)
    _write_report(state.out_dir, "r.json")
    h = _handler(state)
    h._save_qa("r.json", "为什么快？", [{"title": "论证链"}], "因为改了调度。")
    data = json.loads((state.out_dir / "r.json").read_text(encoding="utf-8"))
    assert len(data["qa"]) == 1
    assert data["qa"][0]["question"] == "为什么快？"
    assert data["qa"][0]["module"] == "论证链"
    assert data["qa"][0]["answer"] == "因为改了调度。"
    assert data["qa"][0]["ts"]


def test_save_qa_single_mode(tmp_path):
    jp = tmp_path / "single.json"
    jp.write_text('{"paper_title": "x", "findings": []}', encoding="utf-8")
    h = _handler(None, workbench=False, single_json=jp)
    h._save_qa("", "第二问？", [], "答案。")
    data = json.loads(jp.read_text(encoding="utf-8"))
    assert [m["question"] for m in data["qa"]] == ["第二问？"]


def test_save_qa_silent_on_missing(tmp_path):
    # 报告不存在时不抛异常（历史保存失败不影响已给出的回答）
    from paperatomy.serve import _State
    state = _State(tmp_path)
    h = _handler(state)
    h._save_qa("ghost.json", "q", [], "a")   # 不应抛
