"""LLM 接入层 LangChain 后端的行为契约测试。

重点不是测 LangChain 自身，而是锁住三条对上层有影响的约定：
1. ``LlmClient`` 对外签名/语义不变（``backend`` 仍是 ``http``、无 key 时不启用）；
2. 围栏 JSON 仍能被 ``chat_json`` 容错解析；
3. 异常统一归一化为 ``RuntimeError``，且端点不支持 ``response_format`` 时降级重试。
"""

from __future__ import annotations

import pytest

from sortie_deck.llm import LlmClient
from sortie_deck.llm_chain import LangChainBackend, reset_backend_cache


class _FakeBackend:
    """记录调用并返回预设文本的后端替身。"""

    def __init__(self, reply: str = "hello", exc: Exception | None = None) -> None:
        self.reply = reply
        self.exc = exc
        self.calls: list[dict] = []

    async def complete(self, *, system: str, user: str, temperature: float, json_mode: bool) -> str:
        self.calls.append(
            {"system": system, "user": user, "temperature": temperature, "json_mode": json_mode}
        )
        if self.exc is not None:
            raise self.exc
        return self.reply


@pytest.fixture(autouse=True)
def _clear_backend_cache(monkeypatch):
    """隔离模块级后端缓存与外部环境变量，避免测试之间串味。

    仓库根存在 `.env` 时 pydantic-settings 会读到真实 key，
    因此除了清 os.environ 还要屏蔽 settings 上的字段。
    """
    for key in ("TDT_LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("sortie_deck.settings.settings.llm_api_key", None)
    reset_backend_cache()
    yield
    reset_backend_cache()


async def test_chat_text_routes_to_langchain_backend(monkeypatch):
    fake = _FakeBackend(reply="  answer  ")
    monkeypatch.setattr("sortie_deck.llm_chain.get_backend", lambda **_: fake)

    client = LlmClient(api_key="k", cli_fallback=False)
    out = await client.chat_text(system="sys", user="usr", temperature=0.1)

    assert out == "  answer  "
    assert len(fake.calls) == 1
    assert fake.calls[0]["json_mode"] is False
    assert fake.calls[0]["temperature"] == 0.1


async def test_chat_json_strips_markdown_fences(monkeypatch):
    # 部分模型/CLI 会把 JSON 包在围栏里，chat_json 必须继续容错
    fake = _FakeBackend(reply='```json\n{"track_hint": "express"}\n```')
    monkeypatch.setattr("sortie_deck.llm_chain.get_backend", lambda **_: fake)

    client = LlmClient(api_key="k", cli_fallback=False)
    data = await client.chat_json(system="s", user="u")

    assert data == {"track_hint": "express"}
    assert fake.calls[0]["json_mode"] is True


async def test_backend_property_keeps_http_semantics(monkeypatch):
    # 13 处下游把 backend 写进 UI/meta，换成 langchain 后取值不能变
    client = LlmClient(api_key="k", cli_fallback=False)
    assert client.backend == "http"
    assert client.enabled is True


async def test_disabled_without_api_key_or_cli():
    client = LlmClient(api_key="", cli_fallback=False)
    assert client.enabled is False
    assert client.backend == "none"
    with pytest.raises(RuntimeError):
        await client.chat_text(system="s", user="u")


async def test_httpx_path_is_gone(monkeypatch):
    """回滚开关删除后，任何配置都不会再走到 httpx 直连。"""
    assert not hasattr(LlmClient(api_key="k", cli_fallback=False), "_chat_http")
    with pytest.raises(TypeError):
        LlmClient(api_key="k", cli_fallback=False, impl="httpx")  # type: ignore[call-arg]


async def test_json_mode_falls_back_when_response_format_unsupported(monkeypatch):
    backend = LangChainBackend(
        api_key="k", base_url="http://127.0.0.1:9/v1", model="m", timeout=1.0
    )
    seen: list[bool] = []

    async def fake_invoke(model, system, user, temperature, *, json_mode):
        seen.append(json_mode)
        if len(seen) == 1:
            raise ValueError("Unsupported parameter: 'response_format' is not supported")
        return '{"ok": true}'

    monkeypatch.setattr(backend, "_invoke", fake_invoke)
    out = await backend.complete(system="s", user="u", temperature=0.2, json_mode=True)

    assert out == '{"ok": true}'
    assert seen == [True, True]  # 第一次带 json，降级重试仍要求返回 JSON


async def test_non_json_errors_are_normalized_to_runtime_error(monkeypatch):
    secret = "sk-secret-test-key-123"
    backend = LangChainBackend(
        api_key=secret, base_url="http://127.0.0.1:9/v1", model="m", timeout=1.0
    )

    async def fake_invoke(model, system, user, temperature, *, json_mode):
        raise ValueError("boom")

    monkeypatch.setattr(backend, "_invoke", fake_invoke)
    with pytest.raises(RuntimeError) as exc_info:
        await backend.complete(system="s", user="u", temperature=0.2, json_mode=False)

    assert "boom" in str(exc_info.value)
    # 密钥绝不能出现在异常文本或日志字段里
    assert secret not in str(exc_info.value)
