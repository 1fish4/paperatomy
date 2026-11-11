"""本地服务：报告问答 + 论文工作台。

两种启动方式，对应两种模式：

- ``paperatomy serve out/paper.json`` —— **报告模式**：只服务这一份报告，
  页面右下角可以追问。
- ``paperatomy serve`` —— **工作台模式**：打开一个上传页。
  拖进 PDF，选模板和语言，后台跑完整个流水线（抽取 → 论证链 → 定位 → 渲染），
  完成后报告出现在列表里，点开即读。

为什么上传做在本地服务里，而不是 GitHub Pages 上
------------------------------------------------
静态站没有后端：PDF 解析需要 PyMuPDF，抽取需要模型 key，两者都必须留在本机。
把上传做进 ``serve``，等于给命令行配了一张脸——命令行一个字不用敲，网页上全能做。

key 的边界不变：始终只在本地配置里，浏览器永远接触不到；
页面只跟 ``127.0.0.1`` 说话，其他来源的请求直接拒绝。

材料为什么要压缩
----------------
一篇 35 页综述全文 15 万字符，每问一次就传一遍，既慢又贵。
所以复用抽取阶段那套按章节加权压缩，控制在 6 万字符左右。
代价是每章中段被削掉——这一点必须在 system prompt 里对模型说清，
否则它会在缺失处编造，而这正是本工具最不能容忍的错误。
"""

from __future__ import annotations

import json
import re

_RE_SURVEY_HINT = __import__("re").compile(
    r"\b(a\s+)?(survey|systematic\s+review|literature\s+review)\b",
    __import__("re").I)
import threading
import time
import urllib.parse
import glob
import logging
import uuid
import webbrowser

_LOG = logging.getLogger("paperatomy")
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .llm import LLMConfig, LLMError, chat_stream, load_config

__all__ = ["serve", "build_messages", "SYSTEM_PROMPT", "Task"]

#: 送给模型的材料上限。6 万字符约等于 2 万 token，一次问答几分钱。
MATERIAL_BUDGET = 60_000

#: 保留多少轮历史。再多也不会更聪明，只会更贵。
HISTORY_TURNS = 8

#: 上传 PDF 的大小上限。300 页的书也没这么大。
MAX_UPLOAD_BYTES = 120 * 1024 * 1024

#: 报告目录与 PDF 目录名（相对工作目录）。
OUT_DIRNAME = "out"
PDF_DIRNAME = "papers"

SYSTEM_PROMPT = """\
你在帮一位读者读懂一篇论文。

他已经看过一份自动生成的解剖报告（结构化板块 + 一条论证链），现在针对细节提问。
你要像一位读过这篇论文的同行那样回答：直接、有依据、承认不知道。

规则：

1. **只依据【论文材料】回答。** 材料里没有的，直接说「这篇论文没有提到」，
   不要用领域常识补。补了读者就分不清哪些是论文说的、哪些是你加的。
2. **先给结论，再给依据。** 超过三段的回答通常说明没想清楚。
3. 引用原文时标出页码与章节，格式如 `§4.2 · p.12`；能给出原文句子就更好。
4. 材料是**压缩过**的：每节只保留了开头与结尾，中段被省略。
   如果你判断答案落在被省略的部分，直接说「材料里这段被省掉了，建议看原文 p.X」，
   不要根据上下文猜。**承认缺失比编一个像样的答案有用得多。**
5. 用中文回答，方法与数据集的名字保留英文原词。
6. 不要复述报告里已有的内容，除非用户问的就是它。
"""


def build_messages(
    question: str,
    *,
    material: str,
    title: str = "",
    history: list[dict] | None = None,
    context: list[dict] | None = None,
) -> list[dict]:
    """拼装一次问答。``context`` 是被用户「追问」带进来的模块。"""
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"【论文材料】\n标题：{title or '（未识别）'}\n\n{material}",
        },
        {"role": "assistant", "content": "材料已读完，可以提问。"},
    ]

    for turn in (history or [])[-HISTORY_TURNS:]:
        role = turn.get("role")
        content = str(turn.get("content", "") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    body = question.strip()
    if context:
        blocks = []
        for item in context:
            head = str(item.get("title", "") or "未命名").strip()
            text = str(item.get("text", "") or "").strip()
            if text:
                blocks.append(f"▸ {head}\n{text}")
        if blocks:
            body = "【读者正在追问的部分】\n" + "\n\n".join(blocks) + f"\n\n【问题】\n{body}"

    messages.append({"role": "user", "content": body})
    return messages


# ---------------------------------------------------------------------------
# 任务与状态
# ---------------------------------------------------------------------------

@dataclass
class Task:
    """一次上传分析的进度。前端轮询的就是它。"""

    id: str
    """任务 id，上传时分配。"""

    name: str
    """PDF 原始文件名。"""

    template: str = "survey"
    lang: str = "zh"

    status: str = "queued"
    """``queued`` → ``running`` → ``done`` / ``error``。"""

    stage: str = ""
    """当前阶段名（解析 / 抽取 / 论证链 / 定位 / 渲染）。"""

    detail: str = ""
    """阶段内的最新进度行。"""

    error: str = ""
    report: str = ""
    """完成后的报告 JSON 文件名（相对报告目录）。"""

    started: float = field(default_factory=time.time)

    def public(self) -> dict:
        """给前端看的形状——内部字段不外泄。"""
        return {
            "id": self.id,
            "name": self.name,
            "template": self.template,
            "lang": self.lang,
            "status": self.status,
            "stage": self.stage,
            "detail": self.detail,
            "error": self.error,
            "report": self.report,
            "elapsed": round(time.time() - self.started, 1),
        }


class _State:
    """服务端共享状态：任务表 + 报告材料缓存。

    一次只允许跑一个抽取任务。不是做不到并发，是**不该并发**：
    抽取是带修复循环的顺序流程，两个同时跑只会互相抢限流，
    用户得到的是两个都慢。诚实排掉比假装支持好。
    """

    def __init__(self, root: Path):
        self.root = root
        self.tasks: dict[str, Task] = {}
        self.state_file = self.root / "tasks.state.json"
        self.compare_jobs: dict = {}
        self.last_task: dict | None = None
        try:
            if self.state_file.is_file():
                _st = json.loads(
                    self.state_file.read_text(encoding="utf-8"))
                if _st.get("status") == "running":
                    _st["status"] = "interrupted"
                    self.last_task = _st
                    _LOG.warning("上次任务中断：%s（%s 阶段）",
                                 _st.get("name"), _st.get("stage"))
        except Exception:  # noqa: BLE001 —— 状态文件损坏就当没有
            self.last_task = None
        self.db_path = self.root / "reports.db"
        self._sync_db()
        self._refresh_stale_html()
        # report 文件名 -> (标题, 材料, 错误)；解析一次，之后提问直接用
        self.materials: dict[str, tuple[str, str, str]] = {}
        self.running: str | None = None
        # 图表解读任务状态：file -> none/running/done
        self.explain_jobs: dict[str, str] = {}

    def explain_state(self, file: str) -> str:
        return self.explain_jobs.get(file, "none")

    def tabledata_state(self, file: str) -> str:
        return self.explain_jobs.get("td:" + file, "none")

    def set_tabledata_state(self, file: str, st: str) -> None:
        self.explain_jobs["td:" + file] = st

    def set_explain_state(self, file: str, st: str) -> None:
        self.explain_jobs[file] = st

    # -- 目录 ------------------------------------------------------------

    @property
    def out_dir(self) -> Path:
        d = self.root / OUT_DIRNAME
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def pdf_dir(self) -> Path:
        d = self.root / PDF_DIRNAME
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- 报告列表 ----------------------------------------------------------

    def reports(self) -> list[dict]:
        """扫描报告目录。按修改时间倒序，损坏的文件跳过不炸。"""
        items: list[dict] = []
        for jf in sorted(self.out_dir.glob("*.json"),
                         key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
            except Exception:
                continue
            chain = data.get("chain") or {}
            items.append({
                "file": jf.name,
                "title": str(data.get("paper_title") or jf.stem),
                "template": str((data.get("template") or {}).get("id", "")),
                "pages": int(data.get("pages", 0) or 0),
                "blocks": len(data.get("findings") or []),
                "chain": len(chain.get("nodes") or []),
                "mtime": int(jf.stat().st_mtime),
            })
        return items

    # -- 报告索引（SQLite）-------------------------------------------------

    def _db(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _refresh_stale_html(self) -> None:
        """模板更新后自动重渲染过期产物（渲染不调模型，零成本）。

        ``/report`` 直接把 ``out/*.html`` 加注入返回——若 HTML 是旧模板
        渲染的，新加的前端能力（对照原文面板、返回按钮等）就不会出现。
        以打包模板的 mtime 为准，比它旧的产物一律重建。
        """
        try:
            tpl = Path(__file__).with_name("report_template.html")
            tpl_mtime = tpl.stat().st_mtime
        except OSError:
            return
        refreshed = 0
        for jf in sorted(self.out_dir.glob("*.json")):
            html = jf.with_suffix(".html")
            if html.is_file() and html.stat().st_mtime >= tpl_mtime:
                continue
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                from .cli import _render_from_data
                _render_from_data(jf, data)
                refreshed += 1
            except Exception:  # noqa: BLE001 渲染失败不阻塞启动
                continue
        if refreshed:
            print(f"[serve] 模板已更新，重渲染 {refreshed} 份报告")

    def _sync_db(self) -> None:
        """把 out/*.json 的现状同步进 SQLite（报告数级在百以内，开销小）。"""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS reports ("
            "file TEXT PRIMARY KEY, title TEXT, template TEXT, "
            "pages INTEGER, blocks INTEGER, chain INTEGER, mtime INTEGER)")
        try:
            conn.execute("ALTER TABLE reports ADD COLUMN "
                         "collection TEXT NOT NULL DEFAULT ''")
        except Exception:  # noqa: BLE001 —— 列已存在
            pass
        known = {r[0] for r in conn.execute("SELECT file FROM reports")}
        coll = {r[0]: r[1] for r in
                conn.execute("SELECT file, collection FROM reports")}
        for jf in sorted(self.out_dir.glob("*.json")):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
            except Exception:
                continue
            chain = data.get("chain") or {}
            conn.execute(
                "INSERT OR REPLACE INTO reports VALUES (?,?,?,?,?,?,?,?)",
                (jf.name, str(data.get("paper_title") or jf.stem),
                 str((data.get("template") or {}).get("id", "")),
                 int(data.get("pages", 0) or 0),
                 len(data.get("findings") or []),
                 len(chain.get("nodes") or []),
                 int(jf.stat().st_mtime),
                 coll.get(jf.name, "")))
        present = {p.name for p in self.out_dir.glob("*.json")}
        for fname in list(known):
            if fname not in present:
                conn.execute("DELETE FROM reports WHERE file=?", (fname,))
        conn.commit()
        conn.close()

    def set_collection(self, file: str, name: str) -> None:
        """把报告归入合集（name 空 = 移出）。单归属。"""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE reports SET collection=? WHERE file=?",
                     (name, file))
        conn.commit()
        conn.close()

    def query_reports(self, page: int = 1, size: int = 12,
                      q: str = "", tpl: str = "", col: str = "") -> dict:
        """分页 + 筛选的报告查询。"""
        self._sync_db()
        conn = self._db()
        where, args = [], []
        if q:
            where.append("(title LIKE ? OR file LIKE ?)")
            args += [f"%{q}%", f"%{q}%"]
        if tpl in ("survey", "deep", "skim"):
            where.append("template = ?")
            args.append(tpl)
        if col == "__none__":
            where.append("collection = ''")
        elif col:
            where.append("collection = ?")
            args.append(col)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(
            f"SELECT COUNT(*) FROM reports {where_sql}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM reports {where_sql} "
            "ORDER BY mtime DESC LIMIT ? OFFSET ?",
            args + [size, (page - 1) * size]).fetchall()
        pages = max(1, (total + size - 1) // size)
        week = conn.execute(
            "SELECT COUNT(*) FROM reports WHERE mtime >= ?",
            (int(time.time()) - 7 * 86400,)).fetchone()[0]
        by_tpl = dict(conn.execute(
            "SELECT template, COUNT(*) FROM reports "
            "WHERE template<>'' GROUP BY template").fetchall())
        ncol = conn.execute(
            "SELECT COUNT(DISTINCT collection) FROM reports "
            "WHERE collection<>''").fetchone()[0]
        cols = conn.execute(
            "SELECT collection, COUNT(*) FROM reports WHERE collection<>'' "
            "GROUP BY collection ORDER BY COUNT(*) DESC").fetchall()
        conn.close()
        return {"reports": [dict(r) for r in rows], "total": total,
                "page": page, "pages": pages,
                "collections": [{"name": n, "count": c} for n, c in cols],
                "stats": {"total": total, "week": week,
                          "by_tpl": by_tpl, "collections": ncol}}

    def delete_report(self, file: str) -> int:
        """删除 = 移入回收站（out/.trash/，可在诊断面板恢复）。"""
        path = self.report_path(file)
        if path is None:
            raise ValueError("报告不存在或路径不合法")
        trash = self.out_dir / ".trash"
        trash.mkdir(exist_ok=True)
        n = 0
        for ext in (".json", ".md", ".html"):
            f = path.with_suffix(ext)
            if f.exists():
                f.rename(trash / ("%s~%d%s" % (path.stem, int(time.time()),
                                               ext)))
                n += 1
        conn = self._db()
        conn.execute("DELETE FROM reports WHERE file=?", (file,))
        conn.commit()
        conn.close()
        _LOG.info("report -> trash: %s", file)
        return n

    def trash_list(self) -> list:
        """回收站里的报告（按原文件名聚合）。"""
        trash = self.out_dir / ".trash"
        if not trash.is_dir():
            return []
        items: dict = {}
        for p in sorted(trash.glob("*.json")):
            stem = p.name.split("~")[0]
            items[stem] = items.get(stem, 0) + 1
        return [{"file": k + ".json", "count": v} for k, v in items.items()]

    def restore_report(self, file: str) -> int:
        """从回收站恢复一份报告的三件套。"""
        stem = Path(file).stem
        trash = self.out_dir / ".trash"
        n = 0
        for p in sorted(trash.glob(glob.escape(stem) + "~*")):
            dst = self.out_dir / (stem + p.suffix)
            if dst.exists():
                continue
            p.rename(dst)
            n += 1
        return n

    def report_path(self, file: str) -> Path | None:
        """把请求里的文件名解析成报告目录内的真实路径。

        只接受目录内的 ``*.json``——`..` 与绝对路径在这里被拦下。
        这是本服务唯一的文件读取入口，不能给目录穿越留门。
        """
        if not file or "/" in file or "\\" in file or not file.endswith(".json"):
            return None
        path = (self.out_dir / file).resolve()
        if path.parent != self.out_dir.resolve() or not path.is_file():
            return None
        return path

    # -- 材料缓存 ----------------------------------------------------------

    def material_for(self, file: str) -> tuple[str, str]:
        """返回 ``(标题, 材料)``。解析结果按报告缓存，第二次提问不再读 PDF。"""
        if file in self.materials:
            title, mat, _ = self.materials[file]
            return title, mat

        title, material, err = "", "", ""
        data: dict = {}
        path = self.report_path(file)
        if path is None:
            err = "没有这份报告"
        else:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                err = f"报告 JSON 读取失败：{exc}"
            title = str(data.get("paper_title") or "")
            source = str(data.get("source", "") or "")
            pdf = _resolve(source, path) if source else None
            if pdf is not None:
                try:
                    from .parse import parse

                    paper = parse(pdf)
                    material = paper.condensed(MATERIAL_BUDGET)
                except Exception as exc:
                    err = f"原文解析失败：{exc}"
            else:
                err = "找不到原文 PDF，问答只依据已抽取的内容"

        if not material:
            material = _material_from_report(data)
        self.materials[file] = (title, material, err)
        return title, material

    # -- 上传与任务 --------------------------------------------------------

    def save_upload(self, name: str, blob: bytes) -> Path:
        """保存上传的 PDF，返回落盘路径。名字与内容都做校验。"""
        name = name.replace("\\", "/").split("/")[-1].strip() or "paper.pdf"
        if not name.lower().endswith(".pdf"):
            raise ValueError("只接受 PDF 文件")
        if not blob.startswith(b"%PDF"):
            raise ValueError("文件内容不是 PDF（缺少 %PDF 头），可能保存时出错了")
        if len(blob) > MAX_UPLOAD_BYTES:
            raise ValueError("文件超过 120MB 上限")

        stem = re.sub(r'[<>:"|?*\x00-\x1f]', "_", Path(name).stem).strip() or "paper"
        target = self.pdf_dir / f"{stem}.pdf"
        n = 1
        while target.exists():
            target = self.pdf_dir / f"{stem}-{n}.pdf"
            n += 1
        target.write_bytes(blob)
        return target

    def start_task(self, task: Task, pdf: Path) -> None:
        """登记任务并起后台线程。同一时间只跑一个。"""
        self.tasks[task.id] = task
        if self.running is not None:
            task.status = "error"
            task.error = "已有任务在跑，等它结束再传"
            return
        self.running = task.id
        self._save_task_state(task)
        _LOG.info("task start: %s (%s/%s)", task.name, task.template, task.lang)
        thread = threading.Thread(target=self._run, args=(task, pdf), daemon=True)
        thread.start()

    def _start_compare(self, files: list) -> str:
        """多篇对比：结构对齐 + LLM 综合分析，产单文件 HTML。"""
        key = ",".join(sorted(files))
        job = self.compare_jobs.get(key)
        if job and job.get("state") == "running":
            return key
        self.compare_jobs[key] = {"state": "running"}
        threading.Thread(target=self._run_compare,
                         args=(key, list(files)), daemon=True).start()
        return key

    def _run_compare(self, key: str, files: list) -> None:
        job = self.compare_jobs[key]
        try:
            dicts = []
            for f in files:
                rp = self.report_path(f)
                if rp is None or not rp.is_file():
                    raise ValueError("没有这份报告：" + f)
                dicts.append(json.loads(rp.read_text(encoding="utf-8")))
            if len(dicts) < 2:
                raise ValueError("至少选两份报告")
            from .compare import (compare_many_payload,
                                  to_compare_html_many)
            payload = compare_many_payload(dicts)
            digest = []
            for i, d in enumerate(dicts):
                blocks = {b.get("block_id"): str(b.get("content") or "")
                          for b in (d.get("findings") or [])}
                digest.append(
                    "【论文%d】%s（模板：%s）\n一句话：%s\n问题：%s\n"
                    "方法：%s\n结果：%s\n局限：%s"
                    % (i + 1, d.get("paper_title"),
                       (d.get("template") or {}).get("id", ""),
                       blocks.get("tl_dr", "")[:200],
                       blocks.get("problem", "")[:280],
                       blocks.get("method_overview",
                                  blocks.get("method", ""))[:280],
                       blocks.get("result",
                                  blocks.get("consensus", ""))[:280],
                       blocks.get("limits", "")[:200]))
            prompt = (
                "以下是同一研究领域、不同方法/侧重的 %d 篇论文的分析摘要。"
                "请输出一个 JSON 对象："
                '{"common": "2~3 句概括这几篇论文共同关注的问题与共识", '
                '"table": [{"dim": "对比维度（如方法路线/实验设置/核心结论/'
                '适用场景）", "cells": ["论文1在该维度的具体对比", "论文2…", '
                "…]}], "
                '"verdict": "2~3 句给读者的选读建议与整体评价"}。'
                "对比要具体、有信息量，不要空话；cells 顺序与论文编号一致。"
                "\n\n" + "\n\n".join(digest))
            from .llm import chat, load_config
            raw = chat([{"role": "user", "content": prompt}], load_config())
            m = re.search(r"\{.*\}", raw, re.S)
            analysis = json.loads(m.group()) if m else {}
            payload["analysis"] = analysis
            title = "对比分析：%s" % " vs ".join(
                str(d.get("paper_title"))[:24] for d in dicts)
            html = to_compare_html_many(payload, title)
            name = "compare_%d.html" % int(time.time())
            (self.out_dir / name).write_text(html, encoding="utf-8")
            job["state"] = "done"
            job["url"] = "/comparefile/" + name
            _LOG.info("compare done: %s -> %s", key, name)
        except Exception as exc:  # noqa: BLE001
            job["state"] = "error"
            job["error"] = str(exc)
            _LOG.exception("compare failed: %s", key)

    def _start_demo(self, file: str) -> None:
        """动态讲解：模型按论文核心生成分镜脚本，缓存进报告 JSON。"""
        key = "demo:" + file
        job = self.compare_jobs.get(key)
        if job and job.get("state") == "running":
            return
        self.compare_jobs[key] = {"state": "running"}
        threading.Thread(target=self._run_demo, args=(file,),
                         daemon=True).start()

    def _run_demo(self, file: str) -> None:
        key = "demo:" + file
        job = self.compare_jobs[key]
        try:
            rp = self.report_path(file)
            if rp is None or not rp.is_file():
                raise ValueError("没有这份报告")
            data = json.loads(rp.read_text(encoding="utf-8"))
            if data.get("demo_script"):
                job["state"] = "done"
                return
            blocks = {b.get("block_id"): str(b.get("content") or "")
                      for b in (data.get("findings") or [])}
            material = (
                "标题：%s\n一句话：%s\n问题：%s\n背景与已有做法：%s\n"
                "已有做法的不足：%s\n方法概述：%s\n方法步骤：%s\n"
                "与已有方法的对比：%s\n结果：%s\n局限：%s"
                % (data.get("paper_title"), blocks.get("tl_dr", "")[:200],
                   blocks.get("problem", "")[:360],
                   blocks.get("background", "")[:280],
                   blocks.get("gap", "")[:280],
                   blocks.get("method_overview", "")[:300],
                   blocks.get("method_steps", "")[:300],
                   blocks.get("comparison", "")[:280],
                   blocks.get("result", "")[:300],
                   blocks.get("limits", "")[:200]))
            prompt = (
                "根据这篇论文的核心内容，设计一个『动态讲解』分镜脚本："
                "像动画演示一样把论文讲透。画面要求："
                "①开头 1~2 镜把问题讲透——先展示现状的混乱或浪费（tokens），"
                "再展示关键矛盾或约束冲突（tokens/bars）；"
                "②中间 1~2 镜讲已有方法是怎么做的、哪里不够好"
                "（优先用 compare：左列=已有方法要点，右列=本文对应改进）；"
                "③再用 2~4 镜把本文方法逐步拆开讲（flow/tokens，每镜只讲一步）；"
                "④最后 1~2 镜展示效果（bars/line，用真实数字）。"
                "输出 JSON：{\"title\": \"讲解标题\", \"scenes\": ["
                '{"narration": "本镜旁白（80字内，口语化，讲清楚这一镜在说什么）", '
                '"viz": {"type": "tokens|flow|bars|line|compare 之一", '
                '"caption": "画面标题（12字内）", '
                "// tokens: 元素分组堆放，groups:[{label,color(red|teal|amber|gray),count}] 2~5组"
                "// flow: 流程逐步点亮，steps:[3~5步短句]"
                "// bars: 柱状生长对比，items:[{label,value,tag(before|after|none)}] 2~4项, unit:数字单位"
                "// line: 趋势折线，x:[3~10个标签], y:[对应数值], unit:数字单位"
                '// compare: 左右对比，left:{title,items:[2~3条]}, right:{title,items:[2~3条]}'
                "}, …] 。"
                "旁白是整个讲解的灵魂，必须像科普讲解员对着一屋子人讲话："
                "①用短句，一句一个意思，禁止一句套一句的学术长句；"
                "②禁止堆术语——必须出现的方法名/指标名可以保留，"
                "但前后全是大白话，比如不说『针对给定映射与顺序的调度』，"
                "说『任务在哪台机器上跑早就定死了』；"
                "③每镜之间要有承接，像讲故事一样推进："
                "『现在你看到的…』『问题就出在这』『于是作者想到…』『结果呢？』『更妙的是』；"
                "④给出感受和对比（『看着就头疼』『差了整整一倍』），不要干巴巴罗列事实；"
                "⑤长度 50~90 字，宁短勿长。"
                "旁白好坏示例（差=学术腔，好=讲解员）：\n"
                "差：针对给定映射与顺序的工作流调度，本文提出碳感知方法优化碳排放。\n"
                "好：任务在哪台机器上跑、什么时候跑，早就定死了——想躲开高碳电？门都没有。\n"
                "差：实验结果表明该方法相较于基线有显著提升。\n"
                "好：1088 个实例跑下来，碳排放中位数直接砍到基线的六成。\n"
                "镜头数量由论文内容决定：讲到观众能看明白"
                "问题、已有方法、本文方法、效果为止——内容多就多几镜"
                "（最多 14 镜），内容简单就少几镜；不要为凑数拆碎，"
                "也不要压掉关键步骤。"
                "数字必须来自摘要材料，不要编造。\n\n" + material)
            from .llm import chat, load_config
            raw = chat([{"role": "user", "content": prompt}], load_config(),
                       json_mode=True)
            m = re.search(r"\{.*\}", raw, re.S)
            script = json.loads(m.group()) if m else {}
            script = self._norm_demo(script)
            if not script.get("scenes"):
                raise ValueError("分镜脚本解析失败")
            data["demo_script"] = script
            rp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                          encoding="utf-8")
            job["state"] = "done"
            _LOG.info("demo generated: %s (%d scenes)",
                      file, len(script["scenes"]))
        except Exception as exc:  # noqa: BLE001
            job["state"] = "error"
            job["error"] = str(exc)
            _LOG.exception("demo failed: %s", file)

    @staticmethod
    def _norm_demo(script: dict) -> dict:
        """分镜规范化：只留四种画面，坏场景降级为纯旁白。"""
        out = []
        for sc in (script.get("scenes") or [])[:14]:
            if not isinstance(sc, dict):
                continue
            narr = str(sc.get("narration") or "").strip()[:160]
            if not narr:
                continue
            v = sc.get("viz") or {}
            t = str(v.get("type") or "")
            nv = {"caption": str(v.get("caption") or "")[:20]}
            try:
                if t == "tokens" and isinstance(v.get("groups"), list):
                    gs = [{"label": str(g.get("label"))[:12],
                           "color": (str(g.get("color")) if g.get("color")
                                     in ("red", "teal", "amber", "gray")
                                     else "gray"),
                           "count": max(1, min(9, int(g.get("count") or 1)))}
                          for g in v["groups"][:5] if isinstance(g, dict)]
                    if len(gs) >= 2:
                        nv.update({"type": "tokens", "groups": gs})
                elif t == "flow" and isinstance(v.get("steps"), list):
                    ss = [str(x)[:18] for x in v["steps"][:5]
                          if str(x).strip()]
                    if len(ss) >= 2:
                        nv.update({"type": "flow", "steps": ss})
                elif t == "bars" and isinstance(v.get("items"), list):
                    its = [{"label": str(x.get("label"))[:12],
                            "value": round(float(x.get("value")), 3),
                            "tag": (str(x.get("tag")) if x.get("tag")
                                    in ("before", "after") else "none")}
                           for x in v["items"][:4] if isinstance(x, dict)]
                    if len(its) >= 2:
                        nv.update({"type": "bars", "items": its,
                                   "unit": str(v.get("unit") or "")[:12]})
                elif t == "compare" and isinstance(v.get("left"), dict) \
                        and isinstance(v.get("right"), dict):
                    lv, rv = v["left"], v["right"]
                    nv.update({"type": "compare",
                               "left": {"title": str(lv.get("title"))[:14],
                                        "items": [str(x)[:18] for x in
                                                  (lv.get("items") or [])[:3]
                                                  if str(x).strip()]},
                               "right": {"title": str(rv.get("title"))[:14],
                                         "items": [str(x)[:18] for x in
                                                   (rv.get("items") or [])[:3]
                                                   if str(x).strip()]}})
                    if len(nv["left"]["items"]) < 1 or \
                            len(nv["right"]["items"]) < 1:
                        nv = {"caption": nv["caption"]}
                elif t == "line" and isinstance(v.get("x"), list) \
                        and isinstance(v.get("y"), list):
                    xs = [str(x)[:10] for x in v["x"][:10]]
                    ys = [round(float(y), 3) for y in v["y"][:10]]
                    if len(xs) >= 3 and len(ys) == len(xs):
                        nv.update({"type": "line", "x": xs, "y": ys,
                                   "unit": str(v.get("unit") or "")[:12]})
            except Exception:  # noqa: BLE001 —— 数据坏就丢弃画面
                pass
            out.append({"narration": narr, "viz": nv})
        return {"title": str(script.get("title") or "动态讲解")[:30],
                "scenes": out}

    def _save_task_state(self, task: Task) -> None:
        """任务状态落盘：重启后能告诉用户上次跑到哪。"""
        try:
            self.state_file.write_text(
                json.dumps(task.public(), ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _run(self, task: Task, pdf: Path) -> None:
        task.status = "running"
        cfg = load_config()
        try:
            if not cfg.api_key and not cfg.is_local():
                raise LLMError("没有配置 API key，先运行 paperatomy config set")

            from .pipeline import analyze_pdf, find_template

            template = find_template(task.template)

            def say(msg: str) -> None:
                task.detail = msg
                for key, stage in (("解析", "解析 PDF"), ("抽取正文", "抽取正文"),
                                   ("论证链", "抽取论证链"), ("证据定位", "证据定位"),
                                   ("参考文献", "文献网络"), ("节点标题", "文献网络"),
                                   ("文献网络", "文献网络"), ("渲染", "渲染报告")):
                    if msg.startswith(key):
                        task.stage = stage

            result = analyze_pdf(
                pdf, template, cfg,
                lang=task.lang,
                out_prefix=self.out_dir / pdf.stem,
                on_progress=say,
            )
            task.stage = "完成"
            task.detail = f"{result.locate.summary()}；报告已生成"
            task.status = "done"
            task.report = result.json_path.name
        except Exception as exc:  # noqa: BLE001 —— 失败原因要给前端可读
            task.status = "error"
            task.stage = "失败"
            task.error = str(exc)
        finally:
            self.running = None
            self._save_task_state(task)
            _LOG.info("task end: %s -> %s %s", task.name, task.status,
                      task.error or "")


def detect_template(paper) -> dict:
    """零模型启发式：判断这篇论文更像综述还是方法论文。

    依据只有两样可靠的东西：首页文本的自述，和参考文献的体量。
    判断错了用户可以在确认框里改——检测只负责给出默认值。
    """
    head = ""
    for s in paper.sections[:1]:
        head += s.text[:1500]
    head = head[:1500]
    ref_chars = sum(s.chars for s in paper.sections if s.is_reference)
    if _RE_SURVEY_HINT.search(head):
        return {"suggest": "survey",
                "reason": "首页自述为综述（survey/review）",
                "title": paper.title}
    if ref_chars > 20000:
        return {"suggest": "survey",
                "reason": f"参考文献章节 {ref_chars // 1000}k 字符，综述体量",
                "title": paper.title}
    return {"suggest": "deep",
            "reason": "按方法/研究型论文处理（未检测到综述特征）",
            "title": paper.title}


def _resolve(source: str, json_path: Path) -> Path | None:
    """把 JSON 里记的来源路径还原成真实文件。"""
    raw = Path(source)
    candidates = [raw] if raw.is_absolute() else [
        raw, json_path.parent / raw, json_path.parent.parent / raw,
    ]
    for cand in candidates:
        if cand.is_file():
            return cand.resolve()
    return None


def _material_from_report(data: dict) -> str:
    """没有 PDF 时的退路：把已抽取的报告内容当作材料。"""
    lines: list[str] = []
    for finding in data.get("findings") or []:
        body = str(finding.get("content", "") or "").strip()
        if not body:
            continue
        lines.append(f"### {finding.get('block_id', '')}\n{body}")
        for ev in finding.get("evidence") or []:
            quote = str(ev.get("quote", "") or "").strip()
            if quote:
                lines.append(f"- 原文 p.{ev.get('page', '?')}：{quote}")
    chain = data.get("chain") or {}
    if chain.get("thesis"):
        lines.append(f"### 论证链主张\n{chain['thesis']}")
    for node in chain.get("nodes") or []:
        lines.append(f"- [{node.get('type')}] {node.get('label')}：{node.get('detail')}")
    return "\n\n".join(lines) or "（没有可用材料）"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """路由一览：

    - ``GET  /``               工作台页（工作台模式）或报告页（单报告模式）
    - ``GET  /report?file=x``  指定报告（仅工作台模式）
    - ``GET  /api/health``     健康与模式信息
    - ``GET  /api/reports``    报告列表（工作台）
    - ``GET  /api/task/<id>``  任务进度（工作台）
    - ``POST /api/upload``     上传 PDF（工作台）
    - ``POST /api/ask``        追问（两种模式都有）
    """

    protocol_version = "HTTP/1.1"
    server_version = "paperatomy"

    state: _State | None = None          # 工作台模式
    single_file: str | None = None       # 单报告模式的报告 json 文件名
    single_json: Path | None = None      # 同上的完整路径（保存追问历史用）
    html: str = ""
    workbench_html: str = ""
    title: str = ""
    material: str = ""
    cfg: LLMConfig | None = None
    port: int = 0
    workbench: bool = False

    # -- 基础 ------------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin") or ""
        if not origin:
            return True
        return origin in {
            f"http://127.0.0.1:{self.port}",
            f"http://localhost:{self.port}",
        }

    def _inject(self, html: str, report: str | None = None) -> str:
        payload = {"port": self.port,
                   "mode": "workbench" if self.workbench else "report"}
        if report:
            payload["report"] = report
        elif self.single_file:
            payload["report"] = self.single_file
        if self.workbench and (report or self.single_file):
            stem = Path(report or self.single_file).stem
            if (self.state.pdf_dir / (stem + ".pdf")).is_file():
                payload["pdf"] = stem + ".pdf"
        blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        return html.replace("window.__PA_SERVE__ = null;",
                            f"window.__PA_SERVE__ = {blob};")

    # -- GET -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        params = {urllib.parse.unquote(k): urllib.parse.unquote(v)
                  for k, v in (kv.split("=", 1) for kv in query.split("&")
                               if "=" in kv)}

        if path in ("/", "/index.html"):
            page = self.workbench_html if self.workbench else self.html
            self._send(200, self._inject(page).encode("utf-8"),
                       "text/html; charset=utf-8; cache-control: no-cache")
            return

        if path == "/report" and self.workbench:
            file = params.get("file", "")
            rp = self.state.report_path(file) if self.state else None
            if rp is None:
                self._json({"ok": False, "error": "没有这份报告"}, 404)
                return
            html_path = rp.with_suffix(".html")
            if not html_path.is_file():
                self._json({"ok": False,
                            "error": "报告页面还没生成，等任务完成"}, 404)
                return
            page = self._inject(html_path.read_text(encoding="utf-8"), report=file)
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8; cache-control: no-cache")
            return

        if path == "/api/tabledata" and self.workbench:
            file = str(params.get("file") or "")
            if not file:
                self._json({"ok": False, "error": "缺 file 参数"}, 400)
                return
            cached, st = self._tabledata_cached(file)
            if cached is not None or st == "running":
                self._json({"ok": True, "tables": cached or [],
                            "state": st})
                return
            self._start_tabledata(file)
            self._json({"ok": True, "tables": [], "state": "running"})
            return

        if path == "/api/start" and self.workbench:
            # 兼容旧缓存页面：浏览器 fetch 曾漏发 POST（v0.9.6 修复），
            # 这里让 GET 也能开跑，same-origin 校验不变
            self._start(params)
            return

        if path == "/api/health":
            self._json({
                "ok": True,
                "mode": "workbench" if self.workbench else "report",
                "model": self.cfg.model if self.cfg else "",
                "title": self._title(),
            })
            return

        if path.startswith("/api/pdfinfo") and self.workbench:
            file = str(params.get("file") or "")
            stem = Path(file).stem
            pdf = self.state.pdf_dir / (stem + ".pdf")
            if not pdf.is_file():
                self._json({"ok": True, "available": False})
                return
            import fitz
            doc = fitz.open(pdf)
            n = doc.page_count
            doc.close()
            self._json({"ok": True, "available": True,
                        "pages": n, "pdf": stem + ".pdf"})
            return

        if path.startswith("/pageimg/") and self.workbench:
            # /pageimg/<stem>/<n>.png —— 懒渲染 + 磁盘缓存
            m = re.match(r"^/pageimg/([\w\-.]+)/([0-9]{1,3})\.png$", path)
            if not m:
                self._json({"ok": False, "error": "路径不合法"}, 404)
                return
            stem, no = m.group(1), int(m.group(2))
            pdf = self.state.pdf_dir / (stem + ".pdf")
            cache = self.state.out_dir / "pages" / stem
            cache.mkdir(parents=True, exist_ok=True)
            img = cache / f"{no}.png"
            if not img.is_file():
                try:
                    import fitz
                    doc = fitz.open(pdf)
                    if 1 <= no <= doc.page_count:
                        pix = doc[no - 1].get_pixmap(dpi=110)
                        pix.save(img)
                    doc.close()
                except Exception as exc:  # noqa: BLE001
                    self._json({"ok": False, "error": str(exc)}, 500)
                    return
            if img.is_file():
                self._send(200, img.read_bytes(), "image/png")
            else:
                self._json({"ok": False, "error": "页码超出范围"}, 404)
            return

        if path == "/api/figures" and self.workbench:
            file = str(params.get("file") or "")
            try:
                out = self._extract_figs(file)
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": str(exc)}, 500)
                return
            self._json(self._figures_payload(file, out))
            return

        if path == "/api/demo" and self.workbench:
            file = str(params.get("file") or "")
            rp = self.state.report_path(file)
            script = None
            if rp and rp.is_file():
                try:
                    script = json.loads(
                        rp.read_text(encoding="utf-8")).get("demo_script")
                except Exception:  # noqa: BLE001
                    script = None
            job = self.state.compare_jobs.get("demo:" + file) or {}
            self._json({"ok": True, "state": job.get("state", "none"),
                        "script": script, "error": job.get("error")})
            return

        if path == "/api/lasttask" and self.workbench:
            self._json({"ok": True, "task": self.state.last_task})
            return

        if path == "/api/trash" and self.workbench:
            self._json({"ok": True, "items": self.state.trash_list()})
            return

        if path == "/api/compare" and self.workbench:
            files = [x for x in str(params.get("files") or "").split(",")
                     if x]
            if len(files) < 2:
                self._json({"ok": False, "error": "至少选两份报告"}, 400)
                return
            key = ",".join(sorted(files))
            job = self.state.compare_jobs.get(key)
            if not job:
                self.state._start_compare(files)
                self._json({"ok": True, "state": "running"})
                return
            self._json({"ok": True, "state": job.get("state"),
                        "url": job.get("url"), "error": job.get("error")})
            return

        if path.startswith("/comparefile/") and self.workbench:
            name = path.split("/", 2)[2]
            if not re.match(r"^compare_\d+\.html$", name):
                self._json({"ok": False, "error": "非法文件名"}, 404)
                return
            _cf = self.state.out_dir / name
            if not _cf.is_file():
                self._json({"ok": False, "error": "对比报告不存在"}, 404)
                return
            self._send(200, _cf.read_bytes(), "text/html; charset=utf-8")
            return

        if path == "/api/modfigs" and self.workbench:
            file = str(params.get("file") or "")
            try:
                figs = self._extract_figs(file)
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": str(exc)}, 500)
                return
            payload = self._figures_payload(file, figs)
            figs2 = payload.get("figures") or figs
            assign = self._fig_assignment(file, figs)
            mods = {}
            for i, a in enumerate(assign):
                if a:
                    mods.setdefault(a, []).append(i)
            td, td_state = self._tabledata_cached(file)
            td_mods = {}
            figs_by_cap = {re.sub(r"\W+", "", str(f.get("cap") or ""))[:40]: a
                           for f, a in zip(figs2, assign)}
            for ti, tb in enumerate(td or []):
                key = re.sub(r"\W+", "", str(tb.get("cap") or ""))[:40]
                mo = figs_by_cap.get(key)
                if mo:
                    td_mods.setdefault(mo, []).append(ti)
            self._json({"ok": True,
                        "figures": [{k: f.get(k) for k in
                                     ("page", "cap", "w", "h", "kind",
                                      "explain")} for f in figs2],
                        "modules": mods,
                        "td": td or [],
                        "td_modules": td_mods,
                        "td_state": td_state,
                        "explain_state": payload.get("explain_state")})
            return

        if path == "/api/figure" and self.workbench:
            file = str(params.get("file") or "")
            try:
                idx = int(params.get("idx") or -1)
            except ValueError:
                idx = -1
            try:
                figs = self._extract_figs(file)
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": str(exc)}, 500)
                return
            if 0 <= idx < len(figs):
                # 直接输出 PNG 字节：<img src> 指向本接口，
                # 返回 JSON 会让浏览器解码失败（v0.11.0 全破图的根因）
                import base64 as _b64
                png = _b64.b64decode(figs[idx].get("b64") or "")
                self._send(200, png, "image/png")
            else:
                self._json({"ok": False, "error": "没有这张图"}, 404)
            return

        if path == "/api/reports" and self.workbench:
            try:
                page = max(1, int(params.get("page", 1) or 1))
            except ValueError:
                page = 1
            try:
                size = min(48, max(1, int(params.get("size", 12) or 12)))
            except ValueError:
                size = 12
            data = self.state.query_reports(
                page=page, size=size, q=str(params.get("q") or ""),
                tpl=str(params.get("tpl") or ""),
                col=str(params.get("col") or ""))
            data["ok"] = True
            self._json(data)
            return

        if path == "/api/diagnose" and self.workbench:
            self._json(self._diagnose())
            return

        if path.startswith("/api/task/") and self.workbench:
            task = self.state.tasks.get(path.rsplit("/", 1)[-1])
            if task is None:
                self._json({"ok": False, "error": "没有这个任务"}, 404)
                return
            self._json({"ok": True, "task": task.public()})
            return

        self._json({"ok": False, "error":
                          f"服务 v{_version()} 不认识该请求（{path}）——页面与服务版本可能不一致，请关闭黑窗口后重新双击启动工作台"}, 404)

    def _title(self) -> str:
        if self.workbench:
            return "论文工作台"
        if self.state and self.single_file:
            rp = self.state.report_path(self.single_file)
            if rp is not None:
                try:
                    data = json.loads(rp.read_text(encoding="utf-8"))
                    return str(data.get("paper_title") or "")
                except Exception:
                    pass
        return self.title

    # -- POST ------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        params = {urllib.parse.unquote(k): urllib.parse.unquote(v)
                  for k, v in (kv.split("=", 1) for kv in query.split("&")
                               if "=" in kv)}

        if path == "/api/upload":
            self._upload(params)
            return
        if path == "/api/start":
            self._start(params)
            return
        if path == "/api/compare" and self.workbench:
            if not self._same_origin():
                self._json({"ok": False, "error": "拒绝跨站请求"}, 403)
                return
            files = [x for x in str(params.get("files") or "").split(",")
                     if x]
            if len(files) < 2:
                self._json({"ok": False, "error": "至少选两份报告"}, 400)
                return
            key = ",".join(sorted(files))
            if not self.state.compare_jobs.get(key):
                self.state._start_compare(files)
            self._json({"ok": True, "state": "running"})
            return

        if path == "/api/demo" and self.workbench:
            if not self._same_origin():
                self._json({"ok": False, "error": "拒绝跨站请求"}, 403)
                return
            file = str(params.get("file") or "")
            if not self.state.report_path(file):
                self._json({"ok": False, "error": "没有这份报告"}, 404)
                return
            self.state._start_demo(file)
            self._json({"ok": True, "state": "running"})
            return

        if path == "/api/restore" and self.workbench:
            if not self._same_origin():
                self._json({"ok": False, "error": "拒绝跨站请求"}, 403)
                return
            file = str(params.get("file") or "")
            try:
                n = self.state.restore_report(file)
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": str(exc)}, 400)
                return
            self._json({"ok": True, "restored": n})
            return

        if path == "/api/collection" and self.workbench:
            if not self._same_origin():
                self._json({"ok": False, "error": "拒绝跨站请求"}, 403)
                return
            file = str(params.get("file") or "")
            name = str(params.get("name") or "").strip()[:40]
            if not file:
                self._json({"ok": False, "error": "缺少 file"}, 400)
                return
            self.state.set_collection(file, name)
            self._json({"ok": True, "collection": name})
            return

        if path == "/api/fix":
            self._fix(params)
            return

        if path == "/api/delete":
            self._delete(params)
            return
        if path == "/api/ask":
            self._ask()
            return
        self._json({"ok": False, "error":
                          f"服务 v{_version()} 不认识该请求（{path}）——页面与服务版本可能不一致，请关闭黑窗口后重新双击启动工作台"}, 404)

    def _upload(self, params: dict) -> None:
        if not self.workbench:
            self._json({"ok": False, "error": "当前不是工作台模式"}, 404)
            return
        if not self._same_origin():
            self._json({"ok": False, "error": "拒绝跨站请求"}, 403)
            return
        if self.state.running is not None:
            self._json({"ok": False, "error": "已有任务在跑，请等它完成"}, 409)
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                raise ValueError("请求体是空的")
            if length > MAX_UPLOAD_BYTES:
                raise ValueError("文件超过 120MB 上限")
            blob = self.rfile.read(length)

            name = params.get("name", "paper.pdf")
            pdf = self.state.save_upload(name, blob)

            # 上传只做「保存 + 类型检测」——开跑要等用户在确认框里点头。
            # 检测是本地解析（几秒），不调模型、不花钱。
            import fitz as _fz
            _doc = _fz.open(pdf)
            _locked = _doc.needs_pass
            _doc.close()
            if _locked:
                raise ValueError("PDF 已加密，请先解除密码再上传")
            from .parse import parse
            paper = parse(pdf)
            if getattr(paper, "chars", 0) < 500:
                raise ValueError(
                    "这份 PDF 几乎没有文本层（可能是扫描版），暂不支持，"
                    "请换文字版 PDF")
            det = detect_template(paper)
            self._json({"ok": True, "file": pdf.name, "detect": det})
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": str(exc)}, 400)

    def _delete(self, params: dict) -> None:
        if not self.workbench:
            self._json({"ok": False, "error": "当前不是工作台模式"}, 404)
            return
        if not self._same_origin():
            self._json({"ok": False, "error": "拒绝跨站请求"}, 403)
            return
        try:
            file = str(params.get("file") or "")
            n = self.state.delete_report(file)
            self._json({"ok": True, "deleted": n})
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": str(exc)}, 400)

    def _start(self, params: dict) -> None:
        """用户在确认框点头后真正开跑。"""
        if not self.workbench:
            self._json({"ok": False, "error": "当前不是工作台模式"}, 404)
            return
        if not self._same_origin():
            self._json({"ok": False, "error": "拒绝跨站请求"}, 403)
            return
        if self.state.running is not None:
            self._json({"ok": False, "error": "已有任务在跑，请等它完成"}, 409)
            return
        try:
            file = str(params.get("file") or "")
            pdf = self.state.pdf_dir / Path(file).name
            if not file or not pdf.is_file():
                raise ValueError("找不到已上传的文件，请重新上传")
            template = str(params.get("template") or "deep")
            if template not in ("survey", "deep", "skim"):
                template = "deep"
            lang = str(params.get("lang") or "zh")
            if lang not in ("zh", "en", "zh+en"):
                lang = "zh"
            task = Task(id=uuid.uuid4().hex[:12],
                        name=Path(file).name,
                        template=template, lang=lang)
            self.state.start_task(task, pdf)
            self._json({"ok": True, "task": task.public()})
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": str(exc)}, 400)

    def _ask(self) -> None:
        if not self._same_origin():
            self._json({"ok": False, "error": "拒绝跨站请求"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
            return

        question = str(payload.get("question", "") or "").strip()
        if not question:
            self._json({"ok": False, "error": "问题是空的"}, 400)
            return

        file = str(payload.get("report") or self.single_file or "")
        if self.workbench and self.state is not None and file:
            title, material = self.state.material_for(file)
        else:
            title, material = _Handler.title, _Handler.material

        messages = build_messages(
            question,
            material=material,
            title=title,
            history=payload.get("history") or [],
            context=payload.get("context") or [],
        )

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(obj: dict) -> None:
            self.wfile.write(
                f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        answer_parts: list[str] = []
        try:
            assert self.cfg is not None
            for delta in chat_stream(messages, self.cfg):
                answer_parts.append(delta)
                emit({"delta": delta})
            emit({"done": True})
        except (BrokenPipeError, ConnectionResetError):
            return
        except LLMError as exc:
            emit({"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            try:
                emit({"error": f"服务端异常：{exc}"})
            except Exception:
                pass
        answer = "".join(answer_parts)
        if answer:
            self._save_qa(file, question, payload.get("context") or [], answer)
        self.close_connection = True


    def _diagnose(self) -> dict:
        """逐报告体检：JSON 完整性、HTML 新旧、按钮在位、PDF 在位、网络/解读缓存。"""
        try:
            tpl_mt = (Path(__file__).with_name("report_template.html")
                      .stat().st_mtime)
        except OSError:
            tpl_mt = 0
        reports = []
        if self.state:
            for jf in sorted(self.state.out_dir.glob("*.json")):
                item = {"file": jf.name, "checks": {}}
                c = item["checks"]
                try:
                    data = json.loads(jf.read_text(encoding="utf-8"))
                    c["json_ok"] = True
                except Exception:
                    c["json_ok"] = False
                    reports.append(item)
                    continue
                title = str(data.get("paper_title") or "")
                c["title_ok"] = bool(title.strip()) and (
                    "insert your" not in title.lower())
                c["blocks_ok"] = bool(data.get("findings"))
                c["chain_ok"] = bool((data.get("chain") or {}).get("nodes"))
                net = data.get("network") or {}
                c["network_ok"] = bool(net.get("nodes"))
                c["explain_ok"] = bool(data.get("figures_explain"))
                c["tabledata_ok"] = bool(data.get("figures_tabledata"))
                stem = jf.stem
                html = jf.with_suffix(".html")
                c["html_exists"] = html.is_file()
                c["html_fresh"] = (html.is_file()
                                   and html.stat().st_mtime >= tpl_mt)
                c["buttons_ok"] = (html.is_file()
                                   and "btnOrig" in html.read_text(
                                       encoding="utf-8", errors="replace"))
                c["pdf_ok"] = (self.state.pdf_dir / (stem + ".pdf")).is_file()
                reports.append(item)
        pending = []
        known = {jf.stem for jf in self.state.out_dir.glob("*.json")} \
            if self.state else set()

        def _has_report(stem: str) -> bool:
            # 报告名可能带去重后缀（x-1.json），前后缀都能对上才算有
            if stem in known:
                return True
            return any(k.startswith(stem + "-") or stem.startswith(k + "-")
                       for k in known)

        if self.state:
            now = time.time()
            for pf in sorted(self.state.pdf_dir.glob("*.pdf"),
                             key=lambda p: p.stat().st_mtime, reverse=True):
                if _has_report(pf.stem):
                    continue
                if now - pf.stat().st_mtime > 7 * 86400:
                    break  # 按新到旧排，超过一周的不再提示
                pending.append(pf.name)
                if len(pending) >= 8:
                    break
        return {"ok": True, "version": _version(), "reports": reports,
                "pending": pending}

    def _fix(self, params: dict) -> None:
        """单项修复：render=重建 HTML；network=补文献网络。network 走后台线程。"""
        if not self.workbench or not self._same_origin():
            self._json({"ok": False, "error": "拒绝"}, 403)
            return
        file = str(params.get("file") or "")
        action = str(params.get("action") or "")
        if self.state is None:
            self._json({"ok": False, "error": "状态未就绪"}, 400)
            return
        if action == "start":
            # 一键开跑：papers/ 里已上传但没跑过的 PDF
            pdf = self.state.pdf_dir / Path(file).name
            if not pdf.is_file():
                self._json({"ok": False, "error": "找不到这份 PDF"}, 404)
                return
            if self.state.running is not None:
                self._json({"ok": False, "error": "已有任务在跑"}, 409)
                return
            from .parse import parse as _parse
            from .llm import LLMConfig, load_config as _load
            try:
                det = detect_template(_parse(pdf))
            except Exception:  # noqa: BLE001 —— 检测失败就按精读跑
                det = {"suggest": "deep"}
            template = det.get("suggest") or "deep"
            if template not in ("survey", "deep", "skim"):
                template = "deep"
            task = Task(id=uuid.uuid4().hex[:12],
                        name=Path(file).name,
                        template=template, lang="zh")
            self.state.start_task(task, pdf)
            self._json({"ok": True, "task": task.public(),
                        "template": template})
            return

        rp = self.state.report_path(file)
        if rp is None or not rp.is_file():
            self._json({"ok": False, "error": "没有这份报告"}, 404)
            return

        if action == "render":
            try:
                from .cli import _render_from_data
                _render_from_data(rp, json.loads(
                    rp.read_text(encoding="utf-8")))
                self._json({"ok": True})
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": str(exc)}, 500)
            return

        if action == "network":
            if self.state.explain_jobs.get("net:" + file) == "running":
                self._json({"ok": True, "state": "running"})
                return
            self.state.explain_jobs["net:" + file] = "running"

            def run() -> None:
                try:
                    from .refnet import build_network
                    from .parse import parse
                    data = json.loads(rp.read_text(encoding="utf-8"))
                    src = (self.state.pdf_dir
                           / (Path(file).stem + ".pdf"))
                    paper = parse(src)
                    net = build_network(
                        paper, on_progress=lambda m: None)
                    data = json.loads(rp.read_text(encoding="utf-8"))
                    data["network"] = net
                    rp.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                    from .cli import _render_from_data
                    _render_from_data(rp, data)
                    self.state.explain_jobs["net:" + file] = "done"
                except Exception:  # noqa: BLE001
                    self.state.explain_jobs["net:" + file] = "failed"

            threading.Thread(target=run, daemon=True).start()
            self._json({"ok": True, "state": "running"})
            return

        self._json({"ok": False, "error": "未知修复项 " + action}, 400)

    def _fig_assignment(self, file: str, figs: list) -> list:
        """图→模块分配：①页码预归（图页命中板块证据页）②模型复核。"""
        rp = self.state.report_path(file) if self.state else None
        data = {}
        if rp and rp.is_file():
            try:
                data = json.loads(rp.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                data = {}
        blocks = [(str(b.get("block_id")), b)
                  for b in (data.get("findings") or [])
                  if b.get("block_id")]
        assign = [None] * len(figs)
        for i, f in enumerate(figs):
            p = f.get("page")
            best, best_n = None, 0
            for bid, b in blocks:
                n = sum(1 for e in (b.get("evidence") or [])
                        if isinstance(e, dict) and e.get("page") == p)
                if n > best_n:
                    best, best_n = bid, n
            assign[i] = best
        ex = data.get("figures_explain2") or []
        valid = {bid for bid, _ in blocks}
        for i, f in enumerate(figs):
            if i >= len(ex):
                break
            e = ex[i]
            if isinstance(e, dict):
                mo = e.get("module")
                if mo and mo != "none" and mo in valid:
                    assign[i] = mo
        return assign

    @staticmethod
    def _pix_blank(pix) -> bool:
        """空白图检测：抽样像素标准差极小 → 无内容（整页白底误判簇）。"""
        try:
            sm = pix.samples
            n = len(sm)
            if not n:
                return True
            step = max(1, n // 4096)
            vals = [sm[i] for i in range(0, n, step)]
            mean = sum(vals) / len(vals)
            var = sum((v - mean) ** 2 for v in vals) / len(vals)
            return var ** 0.5 < 4
        except Exception:  # noqa: BLE001
            return False

    def _extract_figs(self, file: str) -> list:
        """从 PDF 提取插图/表格（矢量簇 + 位图，过滤小装饰图）。

        /api/figures、/api/modfigs、/api/figure 三处共用。"""
        stem = Path(file).stem
        pdf = (self.state.pdf_dir / (stem + ".pdf")) if self.state else None
        if pdf is None or not pdf.is_file():
            return []
        import base64
        import fitz
        out = []
        doc = fitz.open(pdf)
        try:
            for pno in range(min(doc.page_count, 40)):
                page = doc[pno]
                taken: list = []
                # ① 矢量图簇——学术论文的图表大多是矢量绘图，
                #    get_images() 只能抓到位图，纯矢量论文会一张都没有
                try:
                    clusters = [fitz.Rect(r)
                                for r in page.cluster_drawings()]
                except Exception:  # noqa: BLE001
                    clusters = []
                # 嵌套去重：大簇套小簇只留大的（框线图常一层套一层）
                clusters.sort(key=lambda r: -(r.width * r.height))
                _kept = []
                for r in clusters:
                    dup = False
                    for k in _kept:
                        inter = ((min(r.x1, k.x1) - max(r.x0, k.x0))
                                 * (min(r.y1, k.y1) - max(r.y0, k.y0)))
                        a_small = min(r.width * r.height,
                                      k.width * k.height)
                        if a_small > 0 and inter / a_small > 0.55:
                            dup = True
                            break
                    if dup:
                        continue
                    _kept.append(r)
                clusters = _kept
                for r in clusters:
                    if r.width < 140 or r.height < 90:
                        continue
                    # 近整页的「簇」是版面框，不是图
                    if (r.width > page.rect.width * 0.96
                            and r.height > page.rect.height * 0.92):
                        continue
                    pix = page.get_pixmap(clip=r, dpi=150)
                    if pix.width < 280 or pix.height < 180:
                        continue
                    if self._pix_blank(pix):
                        continue
                    taken.append(fitz.Rect(r))
                    b64 = base64.b64encode(pix.tobytes("png")).decode()
                    out.append({"page": pno + 1,
                                "w": pix.width, "h": pix.height,
                                "b64": b64,
                                "cap": _fig_caption(page, r)})
                    if len(out) >= 24:
                        break
                # ② 位图插图，跳过已被矢量簇覆盖的
                for img in page.get_images(full=True):
                    xref = img[0]
                    try:
                        rects = page.get_image_rects(xref)
                        if rects and any(rects[0].intersects(t)
                                         for t in taken):
                            continue
                        pix = fitz.Pixmap(doc, xref)
                        if pix.width < 300 or pix.height < 200:
                            continue
                        if pix.colorspace and pix.colorspace.n > 3:
                            pix = fitz.Pixmap(fitz.csRGB, pix)
                        if self._pix_blank(pix):
                            continue
                        b64 = base64.b64encode(pix.tobytes("png")).decode()
                        rect = rects[0] if rects else None
                        out.append({"page": pno + 1,
                                    "w": pix.width, "h": pix.height,
                                    "b64": b64,
                                    "cap": _fig_caption(page, rect)
                                    if rect is not None else ""})
                        if len(out) >= 24:
                            break
                    except Exception:
                        continue
                if len(out) >= 24:
                    break

        finally:
            doc.close()
        seen = set()
        uniq = []
        for f in out:
            key = (f.get("page"), (f.get("cap") or "").strip())
            if key[1] and key in seen:
                continue
            seen.add(key)
            uniq.append(f)
        return uniq

    def _figures_payload(self, file: str, out: list) -> dict:
        """figures 载荷：kind（figure/table）+ 已缓存的模型解读 + 状态。"""
        state = self.state.explain_state(file) if self.state else "none"
        explains: list = []
        if self.state:
            rp = self.state.report_path(file)
            if rp and rp.is_file():
                try:
                    _d = json.loads(rp.read_text(encoding="utf-8"))
                    raw = _d.get("figures_explain2") or []
                    if not raw:
                        # v1 缓存（字符串列表）照常展示，归属交给页码预归
                        raw = [{"explain": str(x), "module": None}
                               for x in (_d.get("figures_explain") or [])]
                    norm = []
                    for x in raw:
                        if isinstance(x, dict):
                            norm.append({"explain": str(x.get("explain") or "")[:400],
                                         "module": x.get("module")})
                        else:
                            norm.append({"explain": str(x)[:400],
                                         "module": None})
                    # 缓存按当时的提取索引对位；提取去重会移位，
                    # 用 (页码, 题注) 对齐回当前提取，避免张冠李戴
                    slim = _d.get("figures_extract") or []
                    cap_map = {}
                    for j, sf in enumerate(slim):
                        key = (str(sf.get("page")),
                               (sf.get("cap") or "").strip())
                        if key[1] and j < len(norm) and key not in cap_map:
                            cap_map[key] = norm[j]
                    same_len = (len(slim) == len(out) == len(norm)
                                and bool(slim))
                    explains = []
                    for f in out:
                        key = (str(f.get("page")),
                               (f.get("cap") or "").strip())
                        ex = cap_map.get(key)
                        if ex is None and same_len:
                            ex = norm[len(explains)]
                        explains.append(ex or {"explain": "", "module": None})
                except Exception:  # noqa: BLE001
                    explains = []
        for i, f in enumerate(out):
            cap = str(f.get("cap") or "")
            f["kind"] = ("table" if re.match(r"(?i)^\s*(table|表)", cap)
                         else "figure")
            if i < len(explains):
                f["explain"] = (explains[i].get("explain")
                                    if isinstance(explains[i], dict)
                                    else explains[i])        # 首次请求：把提取清单写进报告 JSON，并触发一次批量模型解读
        if out and self.state:
            try:
                rp = self.state.report_path(file)
                if rp and rp.is_file():
                    data = json.loads(rp.read_text(encoding="utf-8"))
                    slim = [{"page": f["page"], "cap": f.get("cap", "")}
                            for f in out]
                    if data.get("figures_extract") != slim:
                        data["figures_extract"] = slim
                        rp.write_text(
                            json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")
                    has_explain = bool(data.get("figures_explain2")) or \
                        bool(data.get("figures_explain"))
                    if (not has_explain
                            and self.state.explain_state(file) == "none"
                            and self.cfg and self.cfg.api_key):
                        self._start_explain(file, slim)
                        state = "running"
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "figures": out, "explain_state": state}

    def _start_explain(self, file: str, figs: list) -> None:
        """后台一次批量模型调用：为每张图生成中文解读并缓存进报告 JSON。"""
        if self.state is None or self.cfg is None:
            return
        if not self.cfg.api_key or self.state.explain_state(file) != "none":
            return
        self.state.explain_jobs[file] = "running"

        def run() -> None:
            try:
                rp = self.state.report_path(file)
                data = json.loads(rp.read_text(encoding="utf-8"))
                # 解读 v2：带图所在页的正文上下文，解读更扎实
                page_ctx = {}
                try:
                    import fitz
                    from .parse import _page_text
                    pdf = self.state.pdf_dir / (Path(file).stem + ".pdf")
                    _doc = fitz.open(pdf)
                    for f in figs:
                        pno = int(f.get("page") or 0)
                        if pno and pno not in page_ctx and pno <= _doc.page_count:
                            page_ctx[pno] = _page_text(
                                _doc[pno - 1], is_first=(pno == 1))[:1500]
                    _doc.close()
                except Exception:  # noqa: BLE001
                    pass
                items = "\n".join(
                    f"{i + 1}. [第{f.get('page')}页] "
                    f"{f.get('cap') or '(无题注)'}"
                    + ("\n   该页正文节选：" + page_ctx[int(f['page'])][:900]
                       if page_ctx.get(int(f.get('page') or 0)) else "")
                    for i, f in enumerate(figs))
                mods = [str(b.get("block_id"))
                        for b in (data.get("findings") or [])
                        if b.get("block_id")]
                mod_list = "、".join(mods) if mods else "（无）"
                prompt = (
                    "下面是一篇论文中提取出的插图/表格清单（含页码、原题注、"
                    "所在页正文节选）。为每一项输出一个 JSON 对象："
                    '{"explain": "用中文写 2~3 句解读：'
                    "①图/表里画了什么（内容与结构）；"
                    "②作者在论文里用它说明什么论点；"
                    '③建议读者结合论文哪部分一起看。", '
                    '"module": "这张图/表最适合放进报告的哪个板块，'
                    "从这些板块编号里选一个：" + mod_list +
                    '；都不合适就填 none"}。'
                    "只依据题注、正文节选与领域常识，不要编造具体数据。"
                    "输出 JSON 数组，每项一个对象，顺序与清单一致。\n\n"
                    + items)
                from .llm import chat
                raw = chat([{"role": "user", "content": prompt}], self.cfg)
                m = re.search(r"\[.*\]", raw, re.S)
                arr = json.loads(m.group()) if m else []
                if isinstance(arr, list) and arr:
                    data = json.loads(rp.read_text(encoding="utf-8"))
                    allowed = set(mods)
                    data["figures_explain2"] = [
                        ({"explain": str(x.get("explain") or "")[:400],
                          "module": (str(x.get("module") or "none")
                                     if str(x.get("module") or "none")
                                     in allowed else "none")}
                         if isinstance(x, dict)
                         else {"explain": str(x)[:400], "module": "none"})
                        for x in arr]
                    rp.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                self.state.explain_jobs[file] = "done"
            except Exception:  # noqa: BLE001
                try:
                    self.state.explain_jobs[file] = "none"
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=run, daemon=True).start()

    def _tabledata_cached(self, file: str) -> tuple[list | None, str]:
        """已缓存的表格数据（来自报告 JSON 的 figures_tabledata）与状态。"""
        if self.state is None:
            return None, "none"
        rp = self.state.report_path(file)
        if rp and rp.is_file():
            try:
                data = json.loads(rp.read_text(encoding="utf-8"))
                td = data.get("figures_tabledata")
                if td:
                    return td, "done"
            except Exception:  # noqa: BLE001
                pass
        return None, self.state.tabledata_state(file)

    def _start_tabledata(self, file: str) -> None:
        """后台批量模型调用：从含表格的页面文本中结构化表格数据。"""
        if self.state is None or self.cfg is None or not self.cfg.api_key:
            return
        if self.state.tabledata_state(file) != "none":
            return
        self.state.set_tabledata_state(file, "running")

        def run() -> None:
            try:
                import fitz
                from .parse import _page_text
                pdf = self.state.pdf_dir / (Path(file).stem + ".pdf")
                doc = fitz.open(pdf)
                chunks = []
                cap_re = re.compile(r"(?i)\b(table|表)\s*\d{1,2}\b")
                for pno in range(min(doc.page_count, 20)):
                    page = doc[pno]
                    text = _page_text(page, is_first=(pno == 0))
                    m = cap_re.search(text)
                    if m:
                        chunks.append(f"[第{pno + 1}页·疑似{m.group(0)}]"
                                      + text[:3200])
                doc.close()
                if not chunks:
                    self.state.set_tabledata_state(file, "none")
                    return
                body = "\n\n====\n\n".join(chunks[:4])
                prompt = (
                    "下面是一篇论文若干页的文本（双栏重排，可能有乱序）。"
                    "找出其中的**数据表格**（Table/表），把每个表格转成 JSON："
                    '{"cap": "表的原题注", "columns": ["列名", ...], '
                    '"rows": [["第一列值", 数字或字符串, ...], ...]}。'
                    "只要数据表格，不要示意图清单；数值尽量转成数字；"
                    "找不到完整数据的表就跳过。输出 JSON 数组，最多 4 个。"
                    "\n\n" + body)
                from .llm import chat
                raw = chat([{"role": "user", "content": prompt}], self.cfg)
                m = re.search(r"\[.*\]", raw, re.S)
                arr = json.loads(m.group()) if m else []
                tables = []
                if isinstance(arr, list):
                    for tb in arr:
                        if not isinstance(tb, dict):
                            continue
                        cols = [str(c) for c in (tb.get("columns") or [])]
                        rows = tb.get("rows") or []
                        if not cols or not rows:
                            continue
                        tables.append({"cap": str(tb.get("cap") or "")[:120],
                                       "columns": cols[:8],
                                       "rows": [list(r)[:8] for r in rows[:14]]})
                        if len(tables) >= 4:
                            break
                if not tables:
                    self.state.set_tabledata_state(file, "none")
                    return
                rp = self.state.report_path(file)
                data = json.loads(rp.read_text(encoding="utf-8"))
                data["figures_tabledata"] = tables
                rp.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                self.state.set_tabledata_state(file, "done")
            except Exception:  # noqa: BLE001
                try:
                    self.state.set_tabledata_state(file, "none")
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=run, daemon=True).start()

    def _save_qa(self, file: str, question: str, context, answer: str) -> None:
        """把问答对追加进报告 JSON 的 ``qa`` 数组，下次打开还在。

        失败静默：历史保存不了不该影响已经送到屏幕上的回答。
        """
        try:
            if self.workbench and self.state is not None and file:
                path = self.state.report_path(file)
            elif self.single_json is not None:
                path = Path(self.single_json)
            else:
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            qa = data.setdefault("qa", [])
            module = "、".join(
                str(c.get("title") or "") for c in (context or [])
                if isinstance(c, dict) and c.get("title"))
            qa.append({
                "module": module[:80],
                "question": str(question)[:500],
                "answer": answer[:8000],
                "ts": time.strftime("%Y-%m-%d %H:%M"),
            })
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

class _SingleBindServer(ThreadingHTTPServer):
    """禁止第二个进程悄悄共用同一端口。

    Windows 的 SO_REUSEADDR 允许两个进程同时 bind 同一端口且连接随机分配——
    旧版服务残留时就会出现「页面是新进程发的、POST 落到旧进程」的错位
    （表现为接口 404「没有这个路径」）。这里显式关掉：端口被占就直接报错，
    由启动脚本负责清场。
    """

    allow_reuse_address = False


def _fig_caption(page, rect) -> str:
    """抓图紧邻下方（或上方）的题注：Fig. x / Table x / 图 x / 表 x。"""
    try:
        blocks = page.get_text("blocks")
    except Exception:  # noqa: BLE001
        return ""
    cands = []
    for b in blocks:
        x0, y0, x1, y1 = b[0], b[1], b[2], b[3]
        text = b[4] if len(b) > 4 else ""
        below_gap = y0 - rect.y1
        above_gap = rect.y0 - y1
        if 0 <= below_gap <= 46 and x1 > rect.x0 - 10:
            cands.append((below_gap, text))
        elif 0 <= above_gap <= 46 and x1 > rect.x0 - 10:
            cands.append((above_gap, text))
    for _, text in sorted(cands, key=lambda p: p[0]):
        s2 = " ".join(str(text).split()).strip()
        if re.match(r"(?i)^(fig(ure)?s?\b|table\b|图\s*\d|表\s*\d)", s2):
            return s2[:120]
    return ""


def _version() -> str:
    """读 pyproject 里的版本号，给 404 兜底信息用。"""
    try:
        import tomllib
        pp = Path(__file__).resolve().parent.parent / "pyproject.toml"
        with open(pp, "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except Exception:  # noqa: BLE001
        return "?"


def _bind(handler: type[_Handler], port: int, host: str = "127.0.0.1",
          tries: int = 12) -> tuple[ThreadingHTTPServer, int]:
    """从 ``port`` 起找一个空闲端口。"""
    last: Exception | None = None
    for candidate in range(port, port + tries):
        try:
            server = _SingleBindServer((host, candidate), handler)
        except OSError as exc:
            last = exc
            continue
        server.daemon_threads = True
        return server, candidate
    raise RuntimeError(f"{port}–{port + tries - 1} 都被占用了：{last}")


def _read_workbench() -> str:
    return (Path(__file__).resolve().parent / "workbench.html").read_text(
        encoding="utf-8")


def serve(
    json_path: str | Path | None = None,
    *,
    root: str | Path | None = None,
    port: int = 8770,
    open_browser: bool = False,
    cfg: LLMConfig | None = None,
) -> int:
    """起服务并阻塞。给 ``json_path`` 进报告模式，不给进工作台模式。"""
    from logging.handlers import RotatingFileHandler
    _log_dir = (Path(root) if root else Path.cwd()) / "logs"
    _log_dir.mkdir(exist_ok=True)
    _rh = RotatingFileHandler(_log_dir / "serve.log", maxBytes=1_000_000,
                              backupCount=5, encoding="utf-8")
    _rh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _LOG.setLevel(logging.INFO)
    _LOG.addHandler(_rh)

    cfg = cfg or load_config()
    if not cfg.api_key and not cfg.is_local():
        print("✗ 没有配置 API key。先跑 paperatomy config set --api-key sk-xxx")
        return 1

    state: _State | None = None
    single_file: str | None = None
    html = ""

    if json_path is not None:
        # 报告模式：与 0.4 的行为一致
        json_path = Path(json_path)
        if not json_path.is_file():
            print(f"✗ 找不到文件：{json_path}")
            return 1
        html_path = json_path.with_suffix(".html")
        if not html_path.is_file():
            print(f"✗ 找不到报告页面：{html_path}")
            print("  先运行 paperatomy render " + str(json_path))
            return 1
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"✗ JSON 解析失败：{exc}")
            return 1

        title = str(data.get("paper_title", "") or "")
        material = ""
        source = str(data.get("source", "") or "")
        if source:
            from .parse import parse

            candidate = _resolve(source, json_path)
            if candidate is not None:
                print(f"读取 {candidate.name} …")
                try:
                    paper = parse(candidate)
                    material = paper.condensed(MATERIAL_BUDGET)
                    print(f"  {paper.total_pages} 页 · 材料 {len(material):,} 字符")
                except Exception as exc:
                    print(f"  ⚠ 重新解析失败：{exc}")
        if not material:
            print("  ⚠ 没有找到原文 PDF，问答将只依据已抽取的报告内容")
            material = _material_from_report(data)

        html = html_path.read_text(encoding="utf-8")
        single_file = json_path.name
        _Handler.title = title
        _Handler.material = material
    else:
        # 工作台模式
        state = _State(Path(root) if root else Path.cwd())
        print(f"工作台就绪：报告目录 {state.out_dir}（已有 {len(state.reports())} 份），"
              f"PDF 目录 {state.pdf_dir}")

    _Handler.state = state
    _Handler.single_file = single_file
    _Handler.single_json = json_path if single_file else None
    _Handler.html = html
    _Handler.workbench_html = _read_workbench() if state is not None else ""
    _Handler.cfg = cfg
    _Handler.workbench = state is not None

    server, actual = _bind(_Handler, port)
    _Handler.port = actual
    url = f"http://127.0.0.1:{actual}/"

    print()
    print("=" * 58)
    if state is not None:
        print(f"  论文工作台：{url}")
        print("  拖一个 PDF 进去就能分析；点报告卡片打开阅读与追问")
    else:
        print(f"  报告已就绪：{url}")
        print("  在页面右下角打开「追问」，就可以针对任何一块提问")
    print(f"  模型：{cfg.model}")
    print("  按 Ctrl+C 结束")
    print("=" * 58)

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.shutdown()
        server.server_close()
    return 0
