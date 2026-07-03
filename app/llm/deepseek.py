"""DeepSeek chat model factory — with reasoning-model support.

DeepSeek exposes an OpenAI-compatible Chat Completions API.  Standard
``ChatOpenAI`` works fine for ``deepseek-chat`` (V3), but reasoning models
(``deepseek-reasoner``, ``deepseek-v4-flash``, …) require two extra things
that the base class doesn't handle:

1. **Streaming capture** — DeepSeek sends ``reasoning_content`` alongside
   ``content`` in every streaming delta.  ``_convert_delta_to_message_chunk``
   ignores unknown fields, so we override
   ``_convert_chunk_to_generation_chunk`` to pull it into
   ``AIMessageChunk.additional_kwargs["reasoning_content"]``.
   Because ``AIMessageChunk.__add__`` calls ``merge_dicts`` on
   ``additional_kwargs``, the string values are *concatenated* as chunks
   accumulate — so the final ``AIMessage`` ends up with the full reasoning
   trace in ``additional_kwargs["reasoning_content"]``.

2. **Multi-turn pass-back** — DeepSeek's API requires that when you replay
   an assistant message that originally contained ``reasoning_content``, you
   include it again (otherwise the API raises 400 "must be passed back").
   ``_convert_message_to_dict`` only serialises known fields, so we override
   ``_get_request_payload`` to inject ``reasoning_content`` back into the
   converted message dicts before the request is sent.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI
from pydantic import Field

from app.core.config import settings


class DeepSeekChatOpenAI(ChatOpenAI):
    """ChatOpenAI subclass that properly handles DeepSeek's reasoning_content.

    Thinking mode notes
    -------------------
    deepseek-v4 系列（如 deepseek-v4-flash / deepseek-v4-pro）默认开启思考模式。
    通过 ``deepseek_enable_thinking`` 字段控制：
    - True  → 显式传 ``{"thinking": {"type": "enabled"}}``（保持默认行为）
    - False → 显式传 ``{"thinking": {"type": "disabled"}}``（强制关闭，减少延迟）

    查询改写场景应始终传入 ``enable_thinking=False``，以节省 token 和时延。
    """

    # 存储在实例上，供 _get_request_payload 读取
    deepseek_enable_thinking: bool = Field(default=True, exclude=True)

    # ------------------------------------------------------------------ #
    # 1. Streaming — capture reasoning_content from each delta chunk
    # ------------------------------------------------------------------ #

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        gen_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if gen_chunk is None:
            return gen_chunk

        choices = chunk.get("choices") or []
        if choices and isinstance(gen_chunk.message, AIMessageChunk):
            delta = choices[0].get("delta") or {}
            rc: str = delta.get("reasoning_content") or ""
            if rc:
                existing = gen_chunk.message.additional_kwargs.get("reasoning_content", "")
                gen_chunk.message.additional_kwargs["reasoning_content"] = existing + rc

        return gen_chunk

    # ------------------------------------------------------------------ #
    # 2. Request payload — re-inject reasoning_content into assistant msgs
    # ------------------------------------------------------------------ #

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        # Collect the original LangChain messages *before* conversion so we
        # can later look up reasoning_content from additional_kwargs.
        original_messages = self._convert_input(input_).to_messages()

        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        # Inject thinking mode control via extra_body (OpenAI SDK merges it
        # verbatim into the JSON body without strict parameter validation).
        thinking_type = "enabled" if self.deepseek_enable_thinking else "disabled"
        extra = dict(payload.get("extra_body") or {})
        extra["thinking"] = {"type": thinking_type}
        payload["extra_body"] = extra

        if "messages" not in payload:
            return payload

        # Walk original messages and the already-converted dicts in lock-step.
        # Both lists should be the same length (conversion is 1:1).
        ai_iter = (
            m for m in original_messages
            if isinstance(m, AIMessage)
        )
        for msg_dict in payload["messages"]:
            if msg_dict.get("role") != "assistant":
                continue
            try:
                ai_msg = next(ai_iter)
            except StopIteration:
                break
            rc = (ai_msg.additional_kwargs or {}).get("reasoning_content")
            if rc:
                msg_dict["reasoning_content"] = rc

        return payload


def get_chat_llm(  # noqa: PLR0913
    *,
    timeout: float | None = None,
    temperature: float | None = None,
    model: str | None = None,
    enable_thinking: bool | None = None,
    base_url: str | None = None,
) -> DeepSeekChatOpenAI:
    """Build a DeepSeek chat model.

    ``enable_thinking`` defaults to ``settings.deepseek_enable_thinking``.
    Pass ``False`` explicitly to disable thinking mode (e.g. for the query
    rewriter where reasoning output is unnecessary and adds latency).
    """
    if not settings.deepseek_api_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY 未配置，无法使用 DeepSeek。请在 .env 中设置后重启。"
        )
    _enable = settings.deepseek_enable_thinking if enable_thinking is None else enable_thinking
    return DeepSeekChatOpenAI(
        model=model or settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        base_url=base_url or settings.deepseek_base_url,
        temperature=settings.llm_temperature if temperature is None else temperature,
        streaming=True,
        # 流式下 timeout 是「单次读」超时（两次 token 之间的最大空闲），不是整条流的总时长：
        # 上游挂起（连上却不吐 token）时据此抛 ReadTimeout，避免 astream_events 永久阻塞。
        timeout=settings.llm_request_timeout if timeout is None else timeout,
        max_retries=settings.llm_max_retries,
        deepseek_enable_thinking=_enable,
    )


__all__ = ["DeepSeekChatOpenAI", "get_chat_llm"]
