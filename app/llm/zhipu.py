"""ZhipuAI (智谱 AI) chat model factory.

ZhipuAI exposes an OpenAI-compatible API at ``https://open.bigmodel.cn/api/paas/v4/``,
so we can reuse ``langchain-openai``'s ``ChatOpenAI`` without any extra dependency.

Thinking / reasoning notes
--------------------------
Model              | Thinking behaviour
------------------ | ------------------------------------------------
GLM-4.6V           | 需显式传 ``{"thinking": {"type": "enabled"}}``
GLM-4.6V-FlashX    | 同上（9B 轻量高速版）
GLM-4.6V-Flash     | 同上（免费版）
GLM-4.7-Flash      | 同上

``ZhipuChatOpenAI`` overrides ``_convert_chunk_to_generation_chunk`` to capture
``reasoning_content`` from streaming deltas (same field as DeepSeek) so the SSE
layer can forward it to the frontend thinking block.

When ``enable_thinking=True`` the factory injects
``model_kwargs={"thinking": {"type": "enabled"}}`` so Flash-series models
actually produce reasoning content.

Vision notes
------------
GLM-4.6V-Flash accepts base64 images as **raw base64 strings** (no
``data:<mime>;base64,`` prefix).  ``_strip_data_prefix`` removes the prefix
before sending, making all ZhipuAI models receive images in their native format.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI
from pydantic import Field

from app.core.config import settings

# Pattern: data:<mime>;base64,<b64data>
_DATA_URL_RE = re.compile(r"^data:([^;]+);base64,(.+)$", re.DOTALL)


def _strip_data_prefix(url: str) -> str:
    """Return just the raw base64 string for a data-URL, or the original URL."""
    m = _DATA_URL_RE.match(url)
    if m:
        return m.group(2)
    return url


def _patch_messages_for_zhipu(messages: list[dict]) -> list[dict]:
    """Walk converted message dicts and strip ``data:`` prefixes from image_url blocks.

    ZhipuAI's GLM-4.6V endpoint (and older GLM-4V) expect raw base64 strings
    rather than full data-URLs inside ``image_url.url``.
    """
    patched: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            patched.append(msg)
            continue
        new_blocks: list[dict] = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "image_url"
                and isinstance(block.get("image_url"), dict)
            ):
                raw_url: str = block["image_url"].get("url", "")
                stripped = _strip_data_prefix(raw_url)
                if stripped != raw_url:
                    block = {
                        **block,
                        "image_url": {**block["image_url"], "url": stripped},
                    }
            new_blocks.append(block)
        patched.append({**msg, "content": new_blocks})
    return patched


class ZhipuChatOpenAI(ChatOpenAI):
    """``ChatOpenAI`` subclass that targets the ZhipuAI OpenAI-compatible endpoint.

    Behavioral differences from the base class:

    1. ``_convert_chunk_to_generation_chunk`` — captures ``reasoning_content``
       from streaming deltas into ``AIMessageChunk.additional_kwargs`` so the
       SSE layer can forward it to the frontend as a thinking block.

    2. ``_get_request_payload`` — injects ``{"thinking": {"type": "enabled"}}``
       directly into the raw JSON body for Flash-series models (bypassing the
       OpenAI SDK's strict parameter validation), and strips ``data:`` prefixes
       from base64 image_url blocks.
    """

    # Extra field stored on the instance so _get_request_payload can read it.
    zhipu_enable_thinking: bool = Field(default=False, exclude=True)

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
    # 2. Request payload — inject thinking param + fix base64 image URLs
    # ------------------------------------------------------------------ #

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        # Strip data: prefixes from image_url blocks (ZhipuAI requires raw base64)
        if "messages" in payload:
            payload["messages"] = _patch_messages_for_zhipu(payload["messages"])
        # Inject thinking parameter via ``extra_body`` — the OpenAI Python SDK
        # rejects non-standard top-level kwargs but merges ``extra_body`` into
        # the JSON body verbatim without validation.
        if self.zhipu_enable_thinking:
            extra = dict(payload.get("extra_body") or {})
            extra["thinking"] = {"type": "enabled"}
            payload["extra_body"] = extra
        return payload


def get_chat_llm(
    *,
    timeout: float | None = None,
    temperature: float | None = None,
    model: str | None = None,
    enable_thinking: bool | None = None,
) -> ZhipuChatOpenAI:
    """Build a ZhipuAI chat model using the configured API key and defaults.

    ``enable_thinking`` defaults to ``settings.zhipuai_enable_thinking``.
    Set to ``False`` to disable the thinking parameter (e.g. for the query
    rewriter where reasoning output is unnecessary and adds latency).
    """
    if not settings.zhipuai_api_key:
        raise RuntimeError(
            "ZHIPUAI_API_KEY 未配置，无法使用智谱 AI。"
            "请在 .env 中设置 ZHIPUAI_API_KEY 后重启。"
        )
    _model = model or settings.zhipuai_model
    _enable = settings.zhipuai_enable_thinking if enable_thinking is None else enable_thinking

    # Models that require/support explicit thinking opt-in via the parameter.
    # Covers: glm-4.6v, glm-4.6v-flash, glm-4.6v-flashx, glm-4.7-flash,
    #         glm-4.1v-thinking, and any future models in these families.
    # Note: GLM-4.6V (paid, non-Flash) also requires explicit enabling per its
    # official documentation — "混合 thinking 自动开启" was an older behavior.
    _model_lower = _model.lower()
    _needs_thinking_param = any(
        kw in _model_lower for kw in ("4.6v", "flash", "4.7", "4.1v-thinking")
    )

    return ZhipuChatOpenAI(
        model=_model,
        api_key=settings.zhipuai_api_key,
        base_url=settings.zhipuai_base_url,
        temperature=settings.llm_temperature if temperature is None else temperature,
        streaming=True,
        # 流式下 timeout 是「单次读」超时（两次 token 之间最大空闲），用于避免上游挂起把流拖死。
        timeout=settings.llm_request_timeout if timeout is None else timeout,
        max_retries=settings.llm_max_retries,
        zhipu_enable_thinking=_enable and _needs_thinking_param,
    )


__all__ = ["ZhipuChatOpenAI", "get_chat_llm"]
