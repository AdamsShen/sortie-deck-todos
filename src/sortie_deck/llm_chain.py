"""LLM 接入层的 LangChain 实现。

为什么单独一个模块：
``LlmClient`` 是全项目唯一的 LLM 出口，业务侧只依赖 ``chat_json`` / ``chat_text``。
把 ChatModel 的构造、调用、异常归一化收在这里，``llm.py`` 只做后端路由，
既能保证旧的 httpx 实现可原样保留用于回滚，也避免业务模块直接依赖 langchain。

关键约束（与旧 httpx 实现逐条对齐，改动前请确认）：
- 不做隐式重试：``max_retries=0``。旧实现没有重试，加了会重复计费并放大故障。
- 异常统一归一化为 ``RuntimeError``：上游（如 ``pipeline_llm.py``）靠
  ``except Exception`` 降级到启发式，异常类型收窄会让兜底失效。
- 只返回文本，不做 JSON 解析：围栏剥离与 ``json.loads`` 仍由
  ``LlmClient.chat_json`` 负责，保证两种后端行为完全一致。
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 端点不支持 response_format 时的错误特征；命中后去掉该参数重试一次。
_UNSUPPORTED_JSON_HINTS: tuple[str, ...] = (
    "response_format",
    "json_object",
    "json mode",
    "unsupported parameter",
    "unsupported value",
)


def _looks_unsupported_json_mode(exc: BaseException) -> bool:
    """判断异常是否是「端点不支持 response_format / json_object」这一类。

    兼容各家 OpenAI 兼容端点的措辞差异（vLLM、Ollama、DashScope 等报错文本不一），
    因此用关键字匹配而非异常类型判断。
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(hint in text for hint in _UNSUPPORTED_JSON_HINTS)


def _content_to_text(content: Any) -> str:
    """把 ``AIMessage.content`` 归一化成纯文本。

    多数端点返回 str；多模态/新版模型可能返回 content blocks 列表，
    这里拼接其中的文本块，非文本块（图片、工具调用）直接忽略。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return str(content)


class LangChainBackend:
    """一次 chat 调用的 LangChain 封装：ChatOpenAI + 文本提取 + 异常归一化。

    实例可被多个 ``LlmClient`` 复用（见 ``get_backend``），从而复用底层
    httpx 连接池，避免旧实现每次调用都新建 ``AsyncClient`` 的 TLS 握手开销。
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        # 端点 host 只用于日志定位，不含 key 与路径
        self.host = urlparse(base_url).netloc or base_url
        self._plain: Any | None = None
        self._json: Any | None = None

    def _build(self, *, json_mode: bool) -> Any:
        """构造 ChatOpenAI。依赖缺失时抛 RuntimeError 而非 ImportError。

        异常会被上游 ``except Exception`` 兜底降级（LLM 功能不可用但服务仍启动），
        避免一个可选依赖缺失拖垮整个进程。
        """
        try:
            from langchain_openai import ChatOpenAI
        except ModuleNotFoundError as exc:  # pragma: no cover - 依赖缺失只能人工处理
            raise RuntimeError(
                "langchain-openai not installed; pip install 'langchain-openai>=1.0.0'"
            ) from exc

        kwargs: dict[str, Any] = {
            "api_key": self.api_key,
            "base_url": self.base_url,
            "model": self.model,
            "timeout": self.timeout,
            # 与旧 httpx 实现一致：SDK 层不重试
            "max_retries": 0,
        }
        if json_mode:
            # 走 model_kwargs 透传，等效于旧实现的 body["response_format"]
            kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}
        return ChatOpenAI(**kwargs)

    @property
    def plain(self) -> Any:
        if self._plain is None:
            self._plain = self._build(json_mode=False)
        return self._plain

    @property
    def json_mode(self) -> Any:
        if self._json is None:
            self._json = self._build(json_mode=True)
        return self._json

    async def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float,
        json_mode: bool,
    ) -> str:
        """执行一次 chat 调用并返回文本内容。

        ``json_mode=True`` 先带 ``response_format`` 调用；若端点不认识该参数，
        去掉参数重试一次（部分 OpenAI 兼容服务会直接 400）。
        """
        if not json_mode:
            try:
                return await self._invoke(self.plain, system, user, temperature, json_mode=False)
            except Exception as exc:
                raise RuntimeError(self._error_text(exc)) from exc

        try:
            return await self._invoke(self.json_mode, system, user, temperature, json_mode=True)
        except Exception as exc:
            if not _looks_unsupported_json_mode(exc):
                raise RuntimeError(self._error_text(exc)) from exc
            # 降级：端点不支持 json_object，退回普通调用由 chat_json 自己剥围栏
            logger.warning(
                "LLM 端点不支持 response_format，降级为普通调用：model=%s host=%s",
                self.model,
                self.host,
            )
            try:
                return await self._invoke(self.plain, system, user, temperature, json_mode=True)
            except Exception as retry_exc:
                raise RuntimeError(self._error_text(retry_exc)) from retry_exc

    async def _invoke(
        self,
        model: Any,
        system: str,
        user: str,
        temperature: float,
        *,
        json_mode: bool,
    ) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        messages = [SystemMessage(content=system), HumanMessage(content=user)]
        started = time.monotonic()
        try:
            reply = await model.bind(temperature=temperature).ainvoke(messages)
        except Exception as exc:
            # 失败路径必须留痕：只记录可定位的上下文，禁止记录 api_key 与完整 prompt
            logger.warning(
                "LLM 调用失败：model=%s host=%s json_mode=%s elapsed=%.2fs error=%s: %s",
                self.model,
                self.host,
                json_mode,
                time.monotonic() - started,
                type(exc).__name__,
                exc,
            )
            raise
        return _content_to_text(getattr(reply, "content", reply))

    def _error_text(self, exc: BaseException) -> str:
        return f"LangChain LLM call failed ({self.model}@{self.host}): {type(exc).__name__}: {exc}"


# 模块级缓存：key 用 api_key 的 sha256 前缀，避免明文密钥驻留字典键。
# 业务侧有 18 处 `LlmClient()` 每次新建实例，只有跨实例缓存才能真正复用连接池。
_BACKENDS: dict[tuple[str, str, str, float], LangChainBackend] = {}


def get_backend(*, api_key: str, base_url: str, model: str, timeout: float) -> LangChainBackend:
    """按连接参数取（并缓存）后端实例。"""
    key = (
        hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16],
        base_url,
        model,
        float(timeout),
    )
    backend = _BACKENDS.get(key)
    if backend is None:
        backend = LangChainBackend(
            api_key=api_key, base_url=base_url, model=model, timeout=timeout
        )
        _BACKENDS[key] = backend
    return backend


def reset_backend_cache() -> None:
    """清空后端缓存（测试与运行时配置热变更时使用）。"""
    _BACKENDS.clear()
