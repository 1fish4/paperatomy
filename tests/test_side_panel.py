# -*- coding: utf-8 -*-
"""回归测试：侧栏与图进模块的关键绑定。

历史教训：v0.9.6 拼接时按钮 click 绑定被误删过一次（面板点不开）；
v0.11.0 起图表速览面板取消、图改嵌模块——本测试同步锁住新形态。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _tpl() -> str:
    return (ROOT / "paperatomy" / "report_template.html").read_text(
        encoding="utf-8")


def test_side_panel_buttons_bound():
    t = _tpl()
    # 侧栏只剩对照原文；图表速览按钮已随 v0.11.0 移除
    assert ('getElementById("btnOrig").addEventListener("click"') in t, (
        "对照原文按钮的 click 绑定丢失——面板将点不开")
    assert "btnFigs" not in t, "图表速览已取消，模板里不应再引用 btnFigs"
    assert 'getElementById("spClose").addEventListener' in t, "关闭按钮绑定丢失"


def test_side_panel_toggle_present():
    t = _tpl()
    assert "function toggle(which)" in t
    assert "function closePanel()" in t


def test_figs_embedded_in_modules():
    """图进模块：卡片容器、分配渲染、附图区、单图懒加载接口都要在。"""
    t = _tpl()
    assert 'class="mod-figs" data-bid=' in t, "模块图容器丢失"
    assert "function initModFigs" in t, "initModFigs 丢失——图不会进模块"
    assert "function mfigCard" in t
    assert "/api/figure?file=" in t, "单图懒加载接口调用丢失"
    assert 'id="figAnnexBody"' in t, "附图区丢失——未分配的图会凭空消失"
    assert "function renderTdModules" in t, "数据动图进模块的渲染丢失"
    assert "/api/modfigs?file=" in t, "modfigs 接口调用丢失"


def test_workbench_collection_ui():
    """文库：归档按钮、合集筛选、弹窗、接口调用都要在。"""
    w = (ROOT / "paperatomy" / "workbench.html").read_text(encoding="utf-8")
    assert 'id="colMask"' in w, "合集弹窗丢失"
    assert "rpt-col" in w, "卡片归档按钮丢失"
    assert "/api/collection?file=" in w, "合集保存接口调用丢失"
    assert "renderColFilter" in w, "合集筛选行渲染丢失"
