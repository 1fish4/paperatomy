import json
import time
from http.client import HTTPConnection
from importlib import import_module
from threading import Event, Thread
from urllib.parse import quote

import pymupdf

from paperatomy import refnet, serve
from paperatomy.llm import LLMConfig
from paperatomy.model import Extraction, Finding


def test_report_is_available_while_network_runs_and_can_be_retried(tmp_path, monkeypatch):
    state = serve._State(tmp_path)
    pdf = state.pdf_dir / "A paper.pdf"
    with pymupdf.open() as document:
        document.new_page().insert_text((40, 80), "A study of workflow scheduling.")
        document.save(pdf)

    cfg = LLMConfig(api_key="fixture-key")
    monkeypatch.setattr(serve, "load_config", lambda: cfg)
    monkeypatch.setattr(import_module("paperatomy.extract"), "extract", lambda paper, template, model, **options: Extraction(
        template_id=template.id, paper_title="A paper",
        findings=[Finding(block_id="tl_dr", content="A study of workflow scheduling.")],
    ))

    def unavailable_chain(*args, **kwargs):
        raise ValueError("fixture")

    monkeypatch.setattr("paperatomy.chain.extract_chain", unavailable_chain)
    entered = Event()
    release = Event()
    attempts = []

    def build(paper, *, on_progress, cfg):
        attempts.append(paper.path)
        on_progress("正在匹配参考文献 1/8 条")
        if len(attempts) == 1:
            entered.set()
            assert release.wait(5)
            return {"nodes": [], "unavailable": "OpenAlex 暂时不可访问"}
        return {"center": {"title": "A paper"}, "unavailable": "",
                "nodes": [refnet.RefNode(title="A referenced paper", kind="cited").to_dict()]}

    monkeypatch.setattr(refnet, "build_network", build)

    class Handler(serve._Handler):
        workbench = True
        workbench_html = serve._read_workbench()

    Handler.state = state
    Handler.cfg = cfg
    file = "A paper.json"
    target = state.out_dir / file
    query = "?file=" + quote(file)
    task = serve.Task(id="task1", name=pdf.name, template="skim", lang="zh")
    with serve.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        Handler.port = server.server_port
        origin = f"http://127.0.0.1:{Handler.port}"
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(path, method="GET"):
            conn = HTTPConnection("127.0.0.1", Handler.port, timeout=3)
            try:
                conn.request(method, path, headers={"Origin": origin})
                response = conn.getresponse()
                assert response.status == 200
                return response.read().decode("utf-8")
            finally:
                conn.close()

        def poll(expected):
            for _ in range(100):
                status = json.loads(request("/api/network" + query))
                if status["state"] == expected:
                    return status
                time.sleep(0.02)
            raise AssertionError(f"network did not enter {expected}")

        try:
            state.start_task(task, pdf)
            assert entered.wait(4)
            assert task.status == "done" and task.report == file
            assert "netBackground" in request("/report" + query)
            assert poll("running")["detail"] == "正在匹配参考文献 1/8 条"
            assert json.loads(target.read_text(encoding="utf-8"))["network"]["background"] == "pending"
            request("/api/network" + query, "POST")
            state.update_report_fields(file, qa=[{"question": "Do not lose this"}],
                                       figures_extract=[{"page": 1}])
            release.set()
            assert "OpenAlex" in poll("failed")["detail"]
            assert len(attempts) == 1
            request("/api/network" + query, "POST")
            assert poll("done")["network"]["nodes"][0]["title"] == "A referenced paper"
            data = json.loads(target.read_text(encoding="utf-8"))
            assert data["qa"][0]["question"] == "Do not lose this"
            assert data["figures_extract"] == [{"page": 1}]
            assert "A referenced paper" in request("/report" + query)
        finally:
            release.set()
            server.shutdown()
            thread.join(timeout=5)


def test_restart_only_queues_persisted_pending_reports(tmp_path, monkeypatch):
    state = serve._State(tmp_path)
    for name, network in (("pending", {"background": "pending", "nodes": []}),
                          ("failed", {"background": "failed"}), ("legacy", None)):
        (state.out_dir / (name + ".json")).write_text(json.dumps({"network": network}), encoding="utf-8")
    queue = []
    monkeypatch.setattr(state, "start_network", lambda file, cfg: queue.append(file))
    state.resume_networks(LLMConfig())
    assert queue == ["pending.json"]
