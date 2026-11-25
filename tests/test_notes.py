import json
from http.client import HTTPConnection
from threading import Thread

from paperatomy import serve
from paperatomy.notes import NoteStore


def test_workbench_evidence_notes_capture_edit_and_persist(tmp_path):
    state = serve._State(tmp_path)
    files = ["First paper with spaces.json", "第二篇.json"]
    evidence = {"page": 7, "para": 3, "section": "Evaluation", "verified": True,
                "quote": "We used identical workload traces.", "quote_zh": "我们使用了相同的工作负载轨迹。"}
    for index, file in enumerate(files):
        (state.out_dir / file).write_text(json.dumps({
            "paper_title": f"Paper {index + 1}",
            "findings": [{"block_id": "result", "title": "实验结果",
                          "evidence": [evidence, {**evidence, "page": 9, "verified": False}]}],
            "chain": {"nodes": [{"id": "step-1", "label": "比较实验条件",
                                "evidence": [{**evidence, "page": 8}]}]},
        }), encoding="utf-8")

    class Handler(serve._Handler):
        workbench = True
        workbench_html = serve._read_workbench()

    Handler.state = state
    with serve.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        Handler.port = server.server_port
        origin = f"http://127.0.0.1:{Handler.port}"
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(path="/api/notes", body=None, source=origin):
            conn = HTTPConnection("127.0.0.1", Handler.port, timeout=3)
            headers = {"Content-Type": "application/json"}
            if source:
                headers["Origin"] = source
            try:
                conn.request("POST" if body is not None else "GET", path,
                             json.dumps(body) if body is not None else None, headers)
                response = conn.getresponse()
                value = response.read().decode("utf-8")
                return response.status, json.loads(value) if path == "/api/notes" else value
            finally:
                conn.close()

        try:
            assert 'href="/notes"' in request("/")[1]
            assert "证据笔记" in request("/notes")[1]
            target = {"file": files[0], "kind": "block", "block": "result",
                      "index": 0, "capture": True}
            assert request(body=target, source="https://elsewhere.example")[0] == 403
            assert request(body=target, source="")[0] == 403
            assert request(body={**target, "file": "../outside.json"})[0] == 400
            assert request(body={**target, "index": 90})[0] == 400
            assert request(body={**target, "expected_quote": "stale report content"})[0] == 400
            status, result = request(body={**target, "quote": "untrusted client value"})
            assert status == 200
            first = result["note"]
            assert first["quote"] == evidence["quote"] and first["quote_zh"] == evidence["quote_zh"]
            assert (first["page"], first["para"], first["verified"]) == (7, 3, True)
            note_id = first["id"]
            assert request(body={"id": note_id, "topic": "调度", "comment": "同一实验输入"})[0] == 200
            repeated = request(body=target)[1]["note"]
            assert (repeated["id"], repeated["comment"], repeated["topic"]) == (
                note_id, "同一实验输入", "调度")
            second = request(body={**target, "file": files[1], "topic": "调度"})[1]["note"]
            assert second["id"] != note_id
            chain = request(body={**target, "kind": "chain", "block": "step-1"})[1]["note"]
            assert (chain["block"], chain["page"]) == ("比较实验条件", 8)
            pending = request(body={**target, "index": 1})[1]["note"]
            assert pending["verified"] is False
            state.notes = NoteStore(state.db_path)
            assert len(request()[1]["notes"]) == 4
            assert {item["file"] for item in request()[1]["notes"] if item["topic"] == "调度"} == set(files)
        finally:
            server.shutdown()
            thread.join(timeout=5)
