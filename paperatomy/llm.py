"""大模型调用：只依赖 OpenAI 兼容接口，不绑定任何一家。

为什么不做 SDK 依赖
-------------------
DeepSeek、通义、Kimi、OpenAI、本地 Ollama 都提供 ``/chat/completions``
兼容接口，用 HTTP 直接调就够。装各家 SDK 只会让依赖膨胀。

为什么默认不带 key
------------------
key 是用户自己的。工具不内置、不代理、不上传。
读取顺序：命令行参数 → 环境变量 → ``~/.paperatomy/config.yaml``。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "LLMConfig",
    "LLMError",
    "load_config",
    "save_config",
    "chat",
    "CONFIG_PATH",
]

#: 配置文件位置。
CONFIG_PATH = Path.home() / ".paperatomy" / "config.yaml"

#: 常见服务商的默认地址，便于用户只填 key。
KNOWN_PROVIDERS = {
    "deepseek": "https://api.deepseek.com/v1",
    "openai": "https://api.openai.com/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "ollama": "http://127.0.0.1:11434/v1",
}


class LLMError(RuntimeError):
    """调用大模型失败。消息为中文，可直接展示给用户。"""


@dataclass
class LLMConfig:
    """调用参数。"""

    api_key: str = ""
    base_url: str = KNOWN_PROVIDERS["deepseek"]
    model: str = "deepseek-chat"
    temperature: float = 0.2
    """抽取任务需要稳定，温度调低。"""
    max_tokens: int = 8000
    timeout: int = 240
    retries: int = 3

    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        # 允许用户只填到域名，自动补 /v1
        if not base.endswith("/v1") and "/v1/" not in base:
            base = base + "/v1"
        return base + "/chat/completions"

    def is_local(self) -> bool:
        return "127.0.0.1" in self.base_url or "localhost" in self.base_url


def load_config(**overrides) -> LLMConfig:
    """按优先级合并配置：显式参数 > 环境变量 > 配置文件 > 默认值。"""
    cfg = LLMConfig()

    # 配置文件
    if CONFIG_PATH.is_file():
        try:
            import yaml
            raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                cfg.api_key = str(raw.get("api_key", "") or "")
                cfg.base_url = str(raw.get("base_url", "") or cfg.base_url)
                cfg.model = str(raw.get("model", "") or cfg.model)
                if raw.get("temperature") is not None:
                    cfg.temperature = float(raw["temperature"])
        except Exception:
            # 配置损坏不应该让主流程崩掉，退化为默认值
            pass

    # 环境变量
    cfg.api_key = os.environ.get("PAPERATOMY_API_KEY", cfg.api_key)
    cfg.base_url = os.environ.get("PAPERATOMY_BASE_URL", cfg.base_url)
    cfg.model = os.environ.get("PAPERATOMY_MODEL", cfg.model)

    # 显式参数（值为 None 时忽略）
    for name in ("api_key", "base_url", "model", "temperature",
                 "max_tokens", "timeout", "retries"):
        value = overrides.get(name)
        if value not in (None, ""):
            setattr(cfg, name, value)

    return cfg


def save_config(cfg: LLMConfig) -> Path:
    """把配置写入用户目录，权限设为仅本人可读。"""
    import yaml

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        yaml.safe_dump(
            {
                "api_key": cfg.api_key,
                "base_url": cfg.base_url,
                "model": cfg.model,
                "temperature": cfg.temperature,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    try:
        CONFIG_PATH.chmod(0o600)
    except Exception:
        pass
    return CONFIG_PATH


# ---------------------------------------------------------------------------
# 调用
# ---------------------------------------------------------------------------

def _post(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    """发 POST 请求，返回解析后的 JSON。优先用 httpx，没有则退回 urllib。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    try:
        import httpx
    except ImportError:
        httpx = None

    if httpx is not None:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, content=body, headers=headers)
            if resp.status_code >= 400:
                raise LLMError(_http_error(resp.status_code, resp.text))
            return resp.json()

    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise LLMError(_http_error(exc.code, detail)) from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"连不上 {url}：{exc.reason}") from exc


def _http_error(code: int, body: str) -> str:
    """把 HTTP 状态码翻译成能照着改的提示。"""
    hint = {
        401: "API key 无效或已过期。检查 PAPERATOMY_API_KEY，或运行 paperatomy config set。",
        403: "没有权限调用该模型。确认 key 所属账号已开通此模型。",
        404: f"接口地址不对。检查 base_url 是否包含正确的路径（多数服务是 .../v1）。",
        429: "触发限流或余额不足。稍后重试，或换一个模型。",
        500: "服务端错误，稍后重试。",
        502: "服务端网关错误，稍后重试。",
        503: "服务暂不可用，稍后重试。",
    }.get(code, "调用失败。")
    snippet = (body or "").strip()[:300]
    return f"HTTP {code} — {hint}\n服务端返回：{snippet}"


def chat(
    messages: list[dict],
    cfg: LLMConfig | None = None,
    *,
    json_mode: bool = True,
) -> str:
    """调用大模型，返回正文。

    参数
    ----
    messages : list[dict]
        OpenAI 格式的消息列表。
    cfg : LLMConfig, 可选
        为空时按默认优先级加载。
    json_mode : bool
        要求模型输出 JSON。不是所有服务都支持，
        失败时自动退回普通模式并在提示词里强调格式。

    异常
    -----
    LLMError
        缺少 key、网络失败、服务端错误，重试耗尽后抛出。
    """
    cfg = cfg or load_config()
    if not cfg.api_key and not cfg.is_local():
        raise LLMError(
            "没有配置 API key。三种方式任选一种：\n"
            "  1. 设置环境变量 PAPERATOMY_API_KEY\n"
            "  2. 运行 paperatomy config set --api-key sk-xxx\n"
            "  3. 命令行加 --api-key sk-xxx"
        )

    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    payload = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    last_error: Exception | None = None
    for attempt in range(cfg.retries):
        try:
            data = _post(cfg.endpoint(), payload, headers, cfg.timeout)
            choices = data.get("choices") or []
            if not choices:
                raise LLMError(f"服务端返回里没有 choices：{str(data)[:200]}")
            content = (choices[0].get("message") or {}).get("content") or ""
            if not content.strip():
                raise LLMError("模型返回了空内容。")
            return content
        except LLMError as exc:
            last_error = exc
            message = str(exc)
            # 这几个错误重试没有意义
            if any(k in message for k in ("401", "403", "没有配置")):
                raise
            # 服务不支持 response_format 时，去掉再试一次
            if json_mode and "response_format" in message:
                payload.pop("response_format", None)
                json_mode = False
                continue
            if attempt < cfg.retries - 1:
                time.sleep(2 * (attempt + 1))
        except Exception as exc:  # 网络抖动等
            last_error = exc
            if attempt < cfg.retries - 1:
                time.sleep(2 * (attempt + 1))

    raise LLMError(f"调用大模型失败（已重试 {cfg.retries} 次）：{last_error}")
