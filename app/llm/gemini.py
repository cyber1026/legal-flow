from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI

from app.core.config import settings


def get_chat_llm(
    *,
    timeout: float | None = None,
    temperature: float | None = None,
    model: str | None = None,
) -> ChatGoogleGenerativeAI:
    """Build a Gemini chat model using the configured API key and defaults.

    ``timeout`` 单位为秒；None 时用 ``settings.llm_request_timeout``。Gemini SDK
    将其作为整体请求超时（与 OpenAI SDK 的「chunk-idle」语义略有差异，但都能避免
    上游卡死把流拖崩）。
    """
    return ChatGoogleGenerativeAI(
        model=model or settings.google_model,
        google_api_key=settings.google_api_key,
        temperature=settings.llm_temperature if temperature is None else temperature,
        timeout=settings.llm_request_timeout if timeout is None else timeout,
    )


def get_default_chat_llm() -> BaseChatModel:
    """Backward-compatible shim — returns the model selected by
    ``settings.llm_provider`` via the unified factory.
    """
    from app.llm.factory import get_default_chat_llm as _factory_default
    return _factory_default()
