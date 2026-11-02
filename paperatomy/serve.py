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
import threading
import time
import urllib.parse
import uuid
import webbrowser
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
        # report 文件名 -> (标题, 材料, 错误)；解析一次，之后提问直接用
        self.materials: dict[str, tuple[str, str, str]] = {}
        self.running: str | None = None

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
        thread = threading.Thread(target=self._run, args=(task, pdf), daemon=True)
        thread.start()

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
                       "text/html; charset=utf-8")
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
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/api/health":
            self._json({
                "ok": True,
                "mode": "workbench" if self.workbench else "report",
                "model": self.cfg.model if self.cfg else "",
                "title": self._title(),
            })
            return

        if path == "/api/reports" and self.workbench:
            self._json({"ok": True, "reports": self.state.reports()})
            return

        if path.startswith("/api/task/") and self.workbench:
            task = self.state.tasks.get(path.rsplit("/", 1)[-1])
            if task is None:
                self._json({"ok": False, "error": "没有这个任务"}, 404)
                return
            self._json({"ok": True, "task": task.public()})
            return

        self._json({"ok": False, "error": "没有这个路径"}, 404)

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
        if path == "/api/ask":
            self._ask()
            return
        self._json({"ok": False, "error": "没有这个路径"}, 404)

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
            template = params.get("template", "survey")
            lang = params.get("lang", "zh")
            if lang not in ("zh", "en", "zh+en"):
                lang = "zh"

            pdf = self.state.save_upload(name, blob)
            task = Task(id=uuid.uuid4().hex[:12], name=name,
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

        try:
            assert self.cfg is not None
            for delta in chat_stream(messages, self.cfg):
                emit({"delta": delta})
            emit({"done": True})
        except LLMError as exc:
            emit({"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            try:
                emit({"error": f"服务端异常：{exc}"})
            except Exception:
                pass
        self.close_connection = True


def _bind(handler: type[_Handler], port: int, host: str = "127.0.0.1",
          tries: int = 12) -> tuple[ThreadingHTTPServer, int]:
    """从 ``port`` 起找一个空闲端口。"""
    last: Exception | None = None
    for candidate in range(port, port + tries):
        try:
            server = ThreadingHTTPServer((host, candidate), handler)
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
