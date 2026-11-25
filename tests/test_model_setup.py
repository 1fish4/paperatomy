import json
from http.client import HTTPConnection
from threading import Thread

import pytest

from paperatomy import llm
from paperatomy import serve


def test_workbench_model_setup(tmp_path, monkeypatch):
    config = tmp_path / "settings" / "config.yaml"
    monkeypatch.setattr(llm, "CONFIG_PATH", config)
    for name in ("API_KEY", "BASE_URL", "MODEL"):
        monkeypatch.delenv("PAPERATOMY_" + name, raising=False)

    rejected = {"bad-key"}
    probes = []

    def probe(messages, cfg, *, json_mode):
        assert messages == [{"role": "user", "content": "Reply with OK."}]
        assert not json_mode
        probes.append((cfg.base_url, cfg.api_key))
        if cfg.api_key in rejected:
            raise llm.LLMError("HTTP 401 — 无效 Key\n服务端返回：" + cfg.api_key)
        return "OK"

    monkeypatch.setattr(serve, "chat", probe)

    class Handler(serve._Handler):
        workbench = True
        cfg = llm.load_config()
        workbench_html = serve._read_workbench()

    with serve.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        Handler.port = server.server_port
        origin = f"http://127.0.0.1:{Handler.port}"
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(method, path, *, body=None, source=None):
            headers = {"Content-Type": "application/json"}
            if source is not None:
                headers["Origin"] = source
            conn = HTTPConnection("127.0.0.1", Handler.port, timeout=5)
            try:
                conn.request(method, path, json.dumps(body) if body else None, headers)
                response = conn.getresponse()
                return response.status, response.read().decode()
            finally:
                conn.close()

        try:
            assert "modelForm" in request("GET", "/")[1]
            assert not json.loads(request("GET", "/api/model")[1])["ready"]
            candidate = {"base_url": "https://model.example/v1", "model": "test"}
            assert request("POST", "/api/model", body=candidate,
                           source="https://other.example")[0] == 403
            assert request("POST", "/api/model", body=candidate,
                           source=origin)[0] == 400
            status, error = request("POST", "/api/model",
                                    body={**candidate, "api_key": "bad-key"}, source=origin)
            assert status == 502 and "401" in error and "bad-key" not in error
            assert not config.exists()
            status, result = request("POST", "/api/model",
                                     body={**candidate, "api_key": "test-key"}, source=origin)
            assert status == 200 and json.loads(result)["ready"] and "test-key" not in result
            assert llm.load_config().api_key == "test-key"
            assert request("POST", "/api/model", body=candidate, source=origin)[0] == 200
            candidate["base_url"] = "https://other.example/v1"
            assert request("POST", "/api/model", body=candidate, source=origin)[0] == 400
            assert llm.load_config().base_url == "https://model.example/v1"

            original = {"profile": "默认配置", "profile_name": "默认配置",
                        "base_url": "https://model.example/v1", "model": "test"}
            second = {"profile": "", "profile_name": "服务商 B",
                      "base_url": "https://other.example/v1", "model": "b",
                      "api_key": "second-key"}
            assert request("POST", "/api/model", body=second, source=origin)[0] == 200
            status, listing = request("GET", "/api/model")
            settings = json.loads(listing)
            assert status == 200 and settings["active_profile"] == "服务商 B"
            assert [item["name"] for item in settings["profiles"]] == ["默认配置", "服务商 B"]
            assert "test-key" not in listing and "second-key" not in listing

            rejected.add("test-key")
            assert request("POST", "/api/model", body=original, source=origin)[0] == 502
            assert probes[-1] == ("https://model.example/v1", "test-key")
            assert llm.load_config().api_key == "second-key"
            rejected.remove("test-key")
            assert request("POST", "/api/model", body=original, source=origin)[0] == 200
            assert llm.load_config().api_key == "test-key"

            wrong_address = {**original, "base_url": second["base_url"]}
            assert request("POST", "/api/model", body=wrong_address, source=origin)[0] == 400
            duplicate = {**original, "profile_name": "服务商 B"}
            assert request("POST", "/api/model", body=duplicate, source=origin)[0] == 409
            assert request("POST", "/api/model", body={**original, "profile": "",
                           "profile_name": "另一账号"}, source=origin)[0] == 400
            assert request("POST", "/api/model", body={**original, "profile_name": "服务商 A"},
                           source=origin)[0] == 200
            active, profiles = llm.load_model_profiles()
            assert active == "服务商 A" and set(profiles) == {"服务商 A", "服务商 B"}
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_legacy_config_is_preserved_when_saving_a_second_named_profile(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(llm, "CONFIG_PATH", path)
    path.write_text("api_key: legacy-test\nbase_url: https://old.example/v1\nmodel: old\n",
                    encoding="utf-8")
    assert llm.load_model_profiles()[0] == "默认配置"
    llm.save_config(llm.LLMConfig(api_key="other-test", model="new"), profile_name="新服务商")
    active, profiles = llm.load_model_profiles()
    assert active == "新服务商"
    assert profiles["默认配置"].api_key == "legacy-test"
    assert profiles["新服务商"].api_key == "other-test"


@pytest.mark.parametrize("url, expected", [
    ("http://127.0.0.1:11434/v1", True),
    ("http://[::1]:11434/v1", True),
    ("https://localhost.example/v1", False),
    ("https://remote.example/127.0.0.1/v1", False),
])
def test_local_model_requires_loopback_hostname(url, expected):
    assert llm.LLMConfig(base_url=url).is_local() is expected
