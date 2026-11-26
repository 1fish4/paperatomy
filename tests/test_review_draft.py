import json
from http.client import HTTPConnection
from threading import Thread

from paperatomy import review_draft, serve
from paperatomy.llm import LLMConfig


def test_review_workbench_keeps_only_supported_paragraphs(tmp_path, monkeypatch):
    state = serve._State(tmp_path)
    files = ["Paper A with spaces.json", "第二篇.json", "Missing evidence.json"]
    for index, file in enumerate(files, 1):
        data = {"paper_title": f"Paper {index}", "findings": [{"block_id": "result", "evidence": [
            {"page": index, "verified": index < 3, "quote": f"Original evidence {index}.",
             "quote_zh": "可核对的译文"},
        ]}]}
        (state.out_dir / file).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    used = []

    def fake_chat(messages, cfg):
        request = json.loads(messages[-1]["content"])
        assert list(request["evidence"]) == ["P1E1", "P2E1"]
        assert len(request["papers"]) == 3
        used.append(request["question"])
        return json.dumps({"sections": [{"heading": "方法与条件", "paragraphs": [
            {"text": "两篇论文采用不同的评估条件。", "refs": ["P1E1", "P2E1", "P1E1"]},
            {"text": "伪造证据", "refs": ["P1E1", "P3E2"]},
            {"text": "无引用段落", "refs": []},
        ]}]})

    monkeypatch.setattr(review_draft, "chat", fake_chat)

    class Handler(serve._Handler):
        workbench = True
        workbench_html = serve._read_workbench()
        cfg = LLMConfig(api_key="fixture")

    Handler.state = state
    with serve.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        Handler.port = server.server_port
        origin = f"http://127.0.0.1:{Handler.port}"
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def call(path, data=None, origin_header=origin):
            connection = HTTPConnection("127.0.0.1", Handler.port, timeout=4)
            try:
                connection.request("POST" if data is not None else "GET", path,
                                   json.dumps(data) if data is not None else None,
                                   {"Origin": origin_header, "Content-Type": "application/json"})
                response = connection.getresponse()
                return response.status, response.read().decode("utf-8")
            finally:
                connection.close()

        try:
            assert 'href="/review"' in call("/")[1]
            assert "导出 Markdown" in call("/review")[1]
            body = {"files": files, "question": " 评估条件如何影响对比？ "}
            assert call("/api/review", body, "https://other.example")[0] == 403
            assert call("/api/review", {**body, "files": [files[0], files[0]]})[0] == 400
            assert call("/api/review", {**body, "files": [files[0], "../bad.json"]})[0] == 400
            status, raw = call("/api/review", body)
            result = json.loads(raw)
            assert status == 200 and result["ok"]
            assert used == ["评估条件如何影响对比？"]
            assert result["sections"][0]["paragraphs"][0]["refs"] == ["P1E1", "P2E1"]
            assert len(result["sections"][0]["paragraphs"]) == 1
            assert result["papers"][2]["evidence_count"] == 0 and not result["papers"][2]["used"]
            assert result["evidence"]["P2E1"]["page"] == 2
            assert "[^P2E1]: Paper 2，第 2 页" in result["markdown"]
            assert "伪造证据" not in result["markdown"] and "无引用段落" not in result["markdown"]
        finally:
            server.shutdown()
            thread.join(timeout=5)
