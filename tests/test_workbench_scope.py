# -*- coding: utf-8 -*-
"""工作台脚本作用域回归测试

背景（v0.17.2）：workbench.html 有三个 <script> 块，第 1 块是 IIFE（私有
state/函数），第 2 块里的按钮绑定曾直接引用第 1 块的私有变量，
点击时抛 ReferenceError、界面毫无反应（用户报「点开始分析没反应」）。
本测试锁死：跨块引用必须走 window.PA 桥接。
"""
import io
import os
import re

W = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "paperatomy", "workbench.html")


def _blocks():
    t = io.open(W, encoding="utf-8").read()
    return t, re.findall(r"<script>(.*?)</script>", t, re.S)


def test_pa_bridge_exists():
    t, _ = _blocks()
    assert "window.PA = {state" in t
    for fn in ("confirmStart", "confirmCancel", "confirmBatchStart",
               "clearSel", "runCompare", "loadReports", "esc"):
        assert re.search(r"window\.PA = \{[^}]*%s" % fn, t), fn


def test_block2_no_bare_iife_refs():
    _, blocks = _blocks()
    assert len(blocks) == 3
    b2 = blocks[2]
    # 裸 state. 引用（排除 PA.state.）
    leftover_state = b2.replace("PA.state.", "").count("state.")
    assert leftover_state == 0, "块2 仍有裸 state. 引用"
    # 裸函数调用（排除 PA.xxx）
    for fn in ("esc", "loadReports", "clearSel", "runCompare",
               "confirmBatchStart", "confirmStart"):
        bare = len(re.findall(r"(?<![\w.])%s\(" % fn, b2))
        assert bare == 0, "块2 仍有裸 %s( 调用" % fn


def test_cfgo_dispatch_via_pa():
    _, blocks = _blocks()
    b2 = blocks[2]
    i = b2.find('getElementById("cfGo")')
    assert i > 0
    seg = b2[i:i + 500]
    assert "PA.state.batchPending" in seg
    assert "PA.confirmBatchStart()" in seg
    assert "PA.confirmStart()" in seg
