# -*- coding: utf-8 -*-
"""回归测试：v0.9.6 的浏览器级 start 请求修复

背景：confirmStart 的 fetch 从 v0.8.1 起漏了 method POST——浏览器发 GET、
服务端只有 POST 路由，确认框流程在浏览器里从未通过（历次「没有这个路径」）。
"""
import io
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_workbench_start_fetch_uses_post():
    w = (ROOT / "paperatomy" / "workbench.html").read_text(encoding="utf-8")
    i = w.find("api/start")
    assert i > 0, "workbench 缺少 /api/start 调用"
    seg = w[i:i + 300]
    assert '{method: "POST"}' in seg, (
        "confirmStart 的 fetch 必须带 method POST——否则浏览器发 GET，"
        "服务端 404（v0.8.1–v0.9.5 的真实事故）")


def test_serve_accepts_start_via_get_and_post():
    s = (ROOT / "paperatomy" / "serve.py").read_text(encoding="utf-8")
    # do_GET 与 do_POST 各有一处 start 路由：兼容旧缓存页面的 GET
    assert s.count('path == "/api/start"') == 2, (
        "/api/start 应同时出现在 do_GET 与 do_POST")
