"""本地问答服务：让报告页面直接追问这篇论文。

为什么起服务，而不是让页面直接调模型
------------------------------------
另一种做法是把 key 存在浏览器的 localStorage 里，页面直接请求模型接口。
那样有三处硬伤：

1. key 会出现在页面里，任何一个 XSS 或恶意扩展都能读走；
2. 多数服务商不允许浏览器直连（CORS 预检过不去）；
3. 每次提问都要把整篇材料从浏览器再传一遍。

放到本地服务里，key 一直留在 ``~/.paperatomy/config.yaml``，
页面只跟 ``127.0.0.1`` 说话，模型调用由服务端完成。

也因此，**静态托管的那份报告天然没有提问功能**。这是有意的：
线上演示不该花别人的钱，也不该让访问者以为自己在跟论文对话。

材料为什么要压缩
----------------
一篇 35 页综述全文 15 万字符，每问一次就传一遍，既慢又贵。
所以复用抽取阶段那套按章节加权压缩，控制在 6 万字符左右。
代价是每章中段被削掉——这一点必须在 system prompt 里对模型说清，
否则它会在缺失处编造，而这正是本工具最不能容忍的错误。
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .llm import LLMConfig, LLMError, chat_stream, load_config
from .parse import Paper

__all__ = ["serve", "build_messages", "SYSTEM_PROMPT"]

#: 送给模型的材料上限。6 万字符约等于 2 万 token，一次问答几分钱。
MATERIAL_BUDGET = 60_000

#: 保留多少轮历史。再多也不会更聪明，只会更贵。
HISTORY_TURNS = 8

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
    """拼装一次问答的消息。

    ``context`` 是被用户「加入对话」的模块——每个模块带标题与正文，
    拼在当前提问之前，模型才知道「这个」指的是哪一块。
    """
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


class _Handler(BaseHTTPRequestHandler):
    """只做三件事：给页面、报健康状态、转发提问。"""

    protocol_version = "HTTP/1.1"
    server_version = "paperatomy"

    html: str = ""
    material: str = ""
    title: str = ""
    cfg: LLMConfig | None = None
    port: int = 0
    ready: bool = True

    # -- 基础 ------------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        """默认实现会把每个请求打到 stderr，吵得看不见别的输出。"""
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
        """拒绝别家网页跨站调用。

        本地服务没有鉴权，若不看 Origin，用户随手打开的任何网页都能
        往 ``127.0.0.1:8770`` 发 POST，把额度刷掉。
        """
        origin = self.headers.get("Origin") or ""
        if not origin:
            return True  # curl、同源导航不带 Origin
        return origin in {
            f"http://127.0.0.1:{self.port}",
            f"http://localhost:{self.port}",
        }

    # -- 路由 ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            # 注入实际端口：HTML 是构建时生成的，它不知道服务跑在哪儿
            page = self.html.replace(
                "window.__PA_SERVE__ = null;",
                f"window.__PA_SERVE__ = {{port:{self.port}}};",
            )
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self._json({
                "ok": True,
                "model": self.cfg.model if self.cfg else "",
                "material_chars": len(self.material),
                "title": self.title,
            })
            return
        self._json({"ok": False, "error": "没有这个路径"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/api/ask":
            self._json({"ok": False, "error": "没有这个路径"}, 404)
            return
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

        messages = build_messages(
            question,
            material=self.material,
            title=self.title,
            history=payload.get("history") or [],
            context=payload.get("context") or [],
        )

        # 先发头再流式写，浏览器才能一边收一边渲染
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(obj: dict) -> None:
            self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            assert self.cfg is not None
            for delta in chat_stream(messages, self.cfg):
                emit({"delta": delta})
            emit({"done": True})
        except LLMError as exc:
            emit({"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            return  # 用户中途关掉页面，不算错误
        except Exception as exc:  # noqa: BLE001
            try:
                emit({"error": f"服务端异常：{exc}"})
            except Exception:
                pass
        self.close_connection = True


def _bind(handler: type[_Handler], port: int, host: str = "127.0.0.1",
          tries: int = 12) -> tuple[ThreadingHTTPServer, int]:
    """从 ``port`` 起找一个空闲端口。

    用户很可能同时开着两份报告，端口被占是常态，
    与其让用户去改参数，不如自己往后挪。
    """
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


def serve(
    json_path: str | Path,
    *,
    port: int = 8770,
    open_browser: bool = False,
    cfg: LLMConfig | None = None,
) -> int:
    """起服务并阻塞。返回退出码。"""
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

    cfg = cfg or load_config()
    if not cfg.api_key and not cfg.is_local():
        print("✗ 没有配置 API key。先跑 paperatomy config set --api-key sk-xxx")
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
        # 退路：没有原文就只用报告本身，问答范围会窄，但要能跑
        print("  ⚠ 没有找到原文 PDF，问答将只依据已抽取的报告内容")
        material = _material_from_report(data)

    _Handler.html = html_path.read_text(encoding="utf-8")
    _Handler.material = material
    _Handler.title = title
    _Handler.cfg = cfg

    server, actual = _bind(_Handler, port)
    _Handler.port = actual
    url = f"http://127.0.0.1:{actual}/"

    print()
    print("=" * 58)
    print(f"  报告已就绪：{url}")
    print(f"  模型：{cfg.model}　材料：{len(material):,} 字符")
    print("  在页面右下角打开「追问」，就可以针对任何一块提问")
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


def _resolve(source: str, json_path: Path) -> Path | None:
    """把 JSON 里记的来源路径还原成真实文件。与 cli 里的逻辑保持一致。"""
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
