# -*- coding: utf-8 -*-
"""回归测试：对照原文/图表速览按钮的 click 绑定必须存在
（v0.9.6 拼接时被误删过一次，用户点不开面板——「看不见了」的真凶之一）
"""
import io
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_side_panel_buttons_bound():
    t = (ROOT / "paperatomy" / "report_template.html").read_text(
        encoding="utf-8")
    for btn in ("btnOrig", "btnFigs"):
        assert ('getElementById("%s").addEventListener("click"' % btn) in t, (
            "%s 的 click 绑定丢失——面板将点不开" % btn)
    assert 'getElementById("spClose").addEventListener' in t, "关闭按钮绑定丢失"


def test_side_panel_toggle_present():
    t = (ROOT / "paperatomy" / "report_template.html").read_text(
        encoding="utf-8")
    assert "function toggle(which)" in t
    assert "function closePanel()" in t
