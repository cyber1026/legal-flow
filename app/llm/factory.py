"""Provider-agnostic chat-model factory.

The active provider is controlled by ``settings.llm_provider`` (env
``LLM_PROVIDER``). Callers should depend on this module rather than importing
a specific provider so swapping providers is a config-only change.
"""

from __future__ import annotations

from functools import lru_cache

from langchain_core.language_models import BaseChatModel

from app.core.config import settings


def get_chat_llm(
    provider: str | None = None,
    temperature: float | None = None,
    model: str | None = None,
    **provider_kwargs,
) -> BaseChatModel:
    """Build a chat model for the given provider.

    Extra keyword arguments (e.g. ``enable_thinking=False``) are forwarded
    to provider-specific factories that accept them; others silently ignore them.
    """
    name = (provider or settings.llm_provider).lower()
    if name == "gemini":
        from app.llm.gemini import get_chat_llm as _gemini_factory
        return _gemini_factory(temperature=temperature, model=model, **provider_kwargs)
    if name == "deepseek":
        from app.llm.deepseek import get_chat_llm as _deepseek_factory
        return _deepseek_factory(temperature=temperature, model=model, **provider_kwargs)
    if name == "zhipuai":
        from app.llm.zhipu import get_chat_llm as _zhipu_factory
        return _zhipu_factory(temperature=temperature, model=model, **provider_kwargs)
    raise ValueError(f"未知的 LLM_PROVIDER: {name!r}（支持 gemini / deepseek / zhipuai）")


@lru_cache(maxsize=1)
def get_default_chat_llm() -> BaseChatModel:
    """Cached default chat model selected by ``settings.llm_provider``."""
    return get_chat_llm()


def reset_default_chat_llm() -> None:
    """Drop the cached default — useful in tests when settings change."""
    get_default_chat_llm.cache_clear()


__all__ = ["get_chat_llm", "get_default_chat_llm", "reset_default_chat_llm"]
