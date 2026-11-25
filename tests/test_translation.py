import json
import time
from http.client import HTTPConnection
from threading import Thread
from urllib.parse import quote

import pymupdf

from paperatomy import serve, translation
from paperatomy.llm import LLMConfig, LLMError


def test_translation_api_resumes_and_preserves_page_order(tmp_path, monkeypatch):
    state = serve._State(tmp_path)
    stem = "Workflow draft"
    pdf = state.pdf_dir / (stem + ".pdf")
    with pymupdf.open() as document:
        for text in ("An introduction.", "The evaluation.", ""):
            page = document.new_page()
            if text:
                page.insert_text((30, 30), text)
        document.save(pdf)
    (state.out_dir / (stem + ".json")).write_text("{}", encoding="utf-8")

    calls = []
    cfg = LLMConfig(api_key="example-key", model="test-model")

    def fake_chat(messages, config, *, json_mode):
        assert json_mode and config.model == cfg.model
        text = messages[-1]["content"].strip()
        calls.append(text)
        if text == "The evaluation." and calls.count(text) == 1:
            raise LLMError("网络超时，example-key")
        return json.dumps({"translation": "引言。" if "introduction" in text else "实验评价。"})

    monkeypatch.setattr(serve, "load_config", lambda: cfg)
    monkeypatch.setattr(translation, "chat", fake_chat)

    class Handler(serve._Handler):
        workbench = True

    Handler.state = state
    query = "/api/translation?file=" + quote(stem + ".json")
    with serve.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        Handler.port = server.server_port
        origin = f"http://127.0.0.1:{Handler.port}"
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(path, method="GET", *, same_origin=True):
            conn = HTTPConnection("127.0.0.1", Handler.port, timeout=3)
            try:
                conn.request(method, path, headers={"Origin": origin} if same_origin else {})
                response = conn.getresponse()
                return response.status, response.read().decode("utf-8")
            finally:
                conn.close()

        def poll(expected):
            for _ in range(80):
                _, body = request(query)
                data = json.loads(body)
                if data["state"] == expected:
                    return data
                time.sleep(0.02)
            raise AssertionError(f"Translation did not enter {expected}")

        try:
            assert "全文对照翻译" in request("/translation")[1]
            assert request("/api/demo")[0] == 404
            assert request(query, "POST", same_origin=False)[0] == 403
            assert request(query, "POST")[0] == 200
            failed = poll("error")
            assert failed["pages"] == [1]
            assert "第 2 页" in failed["error"] and cfg.api_key not in failed["error"]
            first = json.loads(request(query + "&page=1")[1])
            assert first["original"].strip() == "An introduction."
            assert first["text"] == "引言。"
            state.translations = translation.TranslationStore(state.translations.root)
            assert poll("partial")["pages"] == [1]
            assert request(query, "POST")[0] == 200
            assert poll("done")["pages"] == [1, 2, 3]
            assert json.loads(request(query + "&page=3")[1])["no_text"]
            assert request(query + "&page=4")[0] == 400
            assert calls == ["An introduction.", "The evaluation.", "The evaluation."]
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_translation_chunking_keeps_all_source_text():
    source = ("Paper sentence with a formula and references.\n" * 300).rstrip()
    parts = list(translation._chunks(source))
    assert len(parts) > 1
    assert "".join(parts) == source
    assert max(map(len, parts)) <= translation.CHUNK_SIZE
