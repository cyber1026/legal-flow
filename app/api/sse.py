"""Convert LangGraph (deepagents) ``astream_events`` v2 stream into the
shared SSE protocol consumed by the Next.js frontend.

Event protocol (server → client):

| event             | payload                                                            |
|-------------------|--------------------------------------------------------------------|
| ``session``       | ``{session_id, message_id}``                                       |
| ``rewrite``       | ``{query}``                                                        |
| ``tool_call_start`` | ``{call_id, name, args, agent}``                                 |
| ``tool_call_end`` | ``{call_id, name, result_preview, citations[], agent}``            |
| ``think_delta``   | ``{delta, agent}`` (model-emitted reasoning, when available)       |
| ``answer_delta``  | ``{delta, agent}`` (final-answer text token chunk)                 |
| ``review_started`` | ``{contract_id}`` —— supervisor 顶层图 enqueue_review 节点已触发    |
|                    | 后台合同审查；前端据此打开审查 SSE 与左侧面板同步进度。              |
| ``done``          | ``{message_id, citations[]}``                                      |
| ``error``         | ``{message}``                                                      |

The wire format is standard SSE: each event is two lines plus a blank line —
``event: <type>\\ndata: <json>\\n\\n``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from langgraph.types import Command

from app.core.config import settings
from app.core.observability import build_run_config
from app.knowledge.summarize import KB_SUMMARIZE_TAG
from app.retrieval.query_rewrite import QUERY_REWRITE_TAG

logger = logging.getLogger(__name__)

# Providers whose public API endpoints do NOT accept image_url content blocks.
# DeepSeek's four models (v4-flash/pro, chat, reasoner) are text-only via the
# public api.deepseek.com; VL2 is self-hosted only.
# ZhipuAI (GLM-4.6V, GLM-4V) DOES support vision — not included here.
_VISION_UNSUPPORTED_PROVIDERS: frozenset[str] = frozenset({"deepseek"})

# If the agent emits no event for this long we abort the stream and surface an
# error to the client. This protects against upstream LLM rate-limit retries
# that can otherwise hang for minutes.
STREAM_IDLE_TIMEOUT_S = 60.0


def sse_pack(event: str, data: dict[str, Any] | None = None) -> str:
    """Encode a single SSE message (``event: ...\\ndata: ...\\n\\n``)."""
    payload = json.dumps(data or {}, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


# --------------------------------------------------------------------------- #
# LangGraph astream_events -> SSE protocol
# --------------------------------------------------------------------------- #


def _is_tool_call_chunk(chunk: dict[str, Any]) -> bool:
    """Whether the chat-model stream chunk includes tool-call deltas."""
    return bool(chunk.get("tool_call_chunks") or chunk.get("tool_calls"))


def _extract_text(content: Any) -> str:
    """Pull plain text from an AIMessageChunk's `.content`, which may be a
    string OR a list of content-block dicts (Gemini, Anthropic). We treat any
    `type=="thinking"` block separately by ignoring it here."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type")
                if btype in ("thinking", "reasoning"):
                    continue
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content)


def _extract_thinking(content: Any) -> str:
    """Pull `thinking`/`reasoning` content blocks (model-dependent)."""
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("thinking", "reasoning"):
                text = block.get("text") or block.get("thinking") or ""
                if text:
                    parts.append(text)
        return "".join(parts)
    return ""


def _summarise(text: str, limit: int = 8000) -> str:
    text = text.strip().replace("\r", "")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _agent_from_event(ev: dict[str, Any]) -> str:  # noqa: ARG001 - kept for event tagging
    """对话路径只有一个 agent（supervisor）。"""
    return "supervisor"


async def stream_agent_as_sse(
    agent,
    user_message: str,
    *,
    session_id: str,
    message_id: str,
    user_id: str | None = None,
    history: list[dict[str, Any]] | None = None,
    on_done: Any = None,
    images: list[str] | None = None,
    resume: str | None = None,
    extra_state: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    """Run the deep-agent against ``user_message`` (optionally with attached
    ``images``) and yield SSE-encoded events conforming to the protocol
    described at the top of this module.

    Images, when provided, are converted into the OpenAI-compatible multimodal
    ``content`` array (``{"type": "text"/"image_url", ...}``) so any LLM that
    speaks OpenAI vision works out of the box.  If the underlying model does
    not understand images it will error — that surfaces back to the client as
    a normal SSE ``error`` event.

    The optional ``on_done(answer_text, citations)`` callback is awaited (or
    called) after the stream finishes successfully — convenient for persisting
    the assistant message after streaming is complete without coupling this
    module to the session store.
    """
    yield sse_pack("session", {"session_id": session_id, "message_id": message_id})

    # Pre-flight: reject image input for providers that don't support vision.
    if images and settings.llm_provider in _VISION_UNSUPPORTED_PROVIDERS:
        provider_label = settings.llm_provider.upper()
        model_label = settings.deepseek_model if settings.llm_provider == "deepseek" else ""
        yield sse_pack(
            "error",
            {
                "message": (
                    f"当前 LLM 提供商 {provider_label}"
                    + (f"（{model_label}）" if model_label else "")
                    + " 不支持图像输入。"
                    " 请在 .env 中将 LLM_PROVIDER 改为 gemini 或 zhipuai（GLM-4.6V）并重启后端，"
                    " 或移除图片后重新发送纯文字消息。"
                )
            },
        )
        yield sse_pack("done", {"message_id": message_id, "citations": [], "thinking_ms": None})
        return

    messages: list[dict[str, Any]] = list(history or [])
    if images:
        # Multimodal turn — content must be a list of typed content-blocks.
        blocks: list[dict[str, Any]] = []
        if user_message:
            blocks.append({"type": "text", "text": user_message})
        for url in images:
            blocks.append({"type": "image_url", "image_url": {"url": url}})
        messages.append({"role": "user", "content": blocks})
    else:
        messages.append({"role": "user", "content": user_message})

    answer_chunks: list[str] = []
    thinking_chunks: list[str] = []
    last_citations: list[dict[str, Any]] = []
    last_rewritten: str | None = None
    pending_tool_args: dict[str, dict[str, Any]] = {}
    pending_tool_names: dict[str, str] = {}
    pending_tool_starts: dict[str, float] = {}   # run_id → time.monotonic()
    pending_tool_agents: dict[str, str] = {}
    completed_tool_calls: list[dict[str, Any]] = []
    thinking_start_t: float | None = None
    thinking_end_t: float | None = None

    # 按真实到达顺序记录「思考段 / 工具调用」交替时间线，用于持久化后还原 ReAct 过程。
    reasoning_steps: list[dict[str, Any]] = []
    _cur_think_seg: list[str] = []
    _cur_think_agent = "supervisor"
    _tool_step_by_id: dict[str, dict[str, Any]] = {}

    def _flush_think_segment() -> None:
        nonlocal _cur_think_agent
        if _cur_think_seg:
            text = "".join(_cur_think_seg).strip()
            if text:
                reasoning_steps.append({
                    "type": "thinking",
                    "text": text,
                    "agent": _cur_think_agent,
                })
            _cur_think_seg.clear()

    def _append_thinking(delta: str, agent_label: str) -> None:
        nonlocal _cur_think_agent
        if _cur_think_seg and _cur_think_agent != agent_label:
            _flush_think_segment()
        _cur_think_agent = agent_label
        _cur_think_seg.append(delta)

    def _current_response_payload() -> tuple[str, str | None, int | None]:
        full_answer = "".join(answer_chunks).strip()
        full_thinking = "".join(thinking_chunks).strip() or None
        if thinking_start_t is not None:
            thinking_ms = int(
                ((thinking_end_t or time.monotonic()) - thinking_start_t) * 1000
            )
        else:
            thinking_ms = None
        return full_answer, full_thinking, thinking_ms

    persisted_response = False

    async def _persist_current_response() -> tuple[str, str | None, int | None]:
        nonlocal persisted_response
        _flush_think_segment()
        full_answer, full_thinking, thinking_ms = _current_response_payload()
        if persisted_response or not full_answer:
            return full_answer, full_thinking, thinking_ms
        persisted_response = True
        if on_done is not None:
            try:
                maybe_coro = on_done(
                    full_answer,
                    last_citations,
                    full_thinking,
                    completed_tool_calls or None,
                    thinking_ms,
                    reasoning_steps or None,
                )
                if hasattr(maybe_coro, "__await__"):
                    await maybe_coro
            except Exception:
                logger.exception("on_done callback failed")
        return full_answer, full_thinking, thinking_ms

    async def _iter_with_idle_timeout(source) -> AsyncIterator[dict[str, Any]]:
        """Yield events from ``source`` but raise ``TimeoutError`` if no event
        arrives within ``STREAM_IDLE_TIMEOUT_S`` seconds (e.g. the upstream
        LLM is stuck in a rate-limit retry loop).
        """
        iterator = source.__aiter__()
        while True:
            try:
                yield await asyncio.wait_for(iterator.__anext__(), STREAM_IDLE_TIMEOUT_S)
            except StopAsyncIteration:
                return

    try:
        initial_state = {"messages": messages, **(extra_state or {})}
        # HITL：resume 非空时不发新消息，而是用 Command 恢复被 interrupt 暂停的图（从 checkpoint 续跑）。
        graph_input: Any = Command(resume=resume) if resume else initial_state
        configurable = {
            key: value for key, value in (extra_state or {}).items() if value is not None
        }
        # HITL/checkpointer：用 session_id 作为 thread_id，使 interrupt/resume/aget_state 能定位线程。
        configurable["thread_id"] = session_id
        # chat 是最外层 root run：把当前请求的 corr_id 绑为 LangSmith 根 run_id，
        # 使该 trace 与日志可经 corr_id 双向跳转；子工具/LLM 调用自动挂到此 run 下。
        config: dict[str, Any] = build_run_config(
            run_name=f"chat:{message_id}",
            tags=["chat", "supervisor"],
            metadata={
                "session_id": session_id,
                "message_id": message_id,
                "user_id": user_id,
                "contract_id": (extra_state or {}).get("contract_id"),
                "llm_provider": settings.llm_provider,
            },
            root=True,
            recursion_limit=50,
        )
        if configurable:
            config["configurable"] = configurable

        async for ev in _iter_with_idle_timeout(
            agent.astream_events(
                graph_input,
                version="v2",
                config=config,
                subgraphs=True,
            )
        ):
            etype = ev.get("event")
            data = ev.get("data") or {}
            agent_label = _agent_from_event(ev)

            # ---------------- LLM token stream ---------------- #
            if etype == "on_chat_model_stream":
                tags = ev.get("tags") or []
                # Tokens emitted by the in-tool query-rewriter LLM should not
                # be surfaced as answer text; they're an implementation detail.
                if QUERY_REWRITE_TAG in tags or KB_SUMMARIZE_TAG in tags:
                    continue

                chunk = data.get("chunk")
                if chunk is None:
                    continue

                if _is_tool_call_chunk({"tool_call_chunks": getattr(chunk, "tool_call_chunks", None),
                                         "tool_calls": getattr(chunk, "tool_calls", None)}):
                    continue

                text = _extract_text(getattr(chunk, "content", ""))
                if text:
                    answer_chunks.append(text)
                    yield sse_pack("answer_delta", {"delta": text, "agent": agent_label})

                # Thinking content from two sources:
                # 1. Content-block style (Anthropic extended thinking, etc.)
                think = _extract_thinking(getattr(chunk, "content", ""))
                # 2. DeepSeek reasoning models put it in additional_kwargs
                think = think or (getattr(chunk, "additional_kwargs", {}) or {}).get(
                    "reasoning_content", ""
                )
                if think:
                    if thinking_start_t is None:
                        thinking_start_t = time.monotonic()
                    thinking_chunks.append(think)
                    _append_thinking(think, agent_label)
                    yield sse_pack("think_delta", {"delta": think, "agent": agent_label})
                elif text and thinking_start_t is not None and thinking_end_t is None:
                    # First answer token marks the end of the thinking phase.
                    thinking_end_t = time.monotonic()

            # ---------------- Tool start ---------------- #
            elif etype == "on_tool_start":
                name = ev.get("name") or "tool"
                run_id = ev.get("run_id") or ""
                input_value = data.get("input") or {}
                if isinstance(input_value, dict):
                    args = input_value
                else:
                    args = {"input": input_value}
                pending_tool_names[run_id] = name
                pending_tool_args[run_id] = args
                pending_tool_starts[run_id] = time.monotonic()
                pending_tool_agents[run_id] = agent_label
                # 工具开始前，先把它之前的思考收成一个时间线段，保证顺序正确。
                _flush_think_segment()
                tool_step = {
                    "type": "tool", "call_id": run_id, "name": name, "args": args,
                    "result_preview": "", "citations": None, "rewritten": None, "elapsed_ms": None,
                    "agent": agent_label,
                }
                reasoning_steps.append(tool_step)
                _tool_step_by_id[run_id] = tool_step
                yield sse_pack(
                    "tool_call_start",
                    {"call_id": run_id, "name": name, "args": args, "agent": agent_label},
                )

            # ---------------- Tool end ---------------- #
            elif etype == "on_tool_end":
                name = ev.get("name") or "tool"
                run_id = ev.get("run_id") or ""
                output = data.get("output")

                citations: list[dict[str, Any]] = []
                rewritten: str | None = None
                preview = ""

                # `retrieve_documents` returns a ToolMessage with .artifact set
                artifact = getattr(output, "artifact", None) if output is not None else None
                if isinstance(artifact, dict):
                    citations = list(artifact.get("citations") or [])
                    rewritten = artifact.get("rewritten")

                content = getattr(output, "content", output)
                if isinstance(content, str):
                    preview = _summarise(content)
                elif isinstance(content, list):
                    preview = _summarise(_extract_text(content))
                else:
                    preview = _summarise(str(content) if content is not None else "")

                if rewritten:
                    last_rewritten = rewritten

                # Accumulate citations across multiple tool calls in the same
                # turn. ``retrieve_documents`` already produces globally
                # continuous indices via its InjectedState offset, so we just
                # append — the result is a single flat list whose ``index``
                # field matches whatever the LLM writes in its answer.
                if citations:
                    last_citations.extend(citations)

                resolved_name = pending_tool_names.pop(run_id, name)
                resolved_args = pending_tool_args.pop(run_id, {})
                resolved_agent = pending_tool_agents.pop(run_id, agent_label)
                tool_elapsed_ms = int(
                    (time.monotonic() - pending_tool_starts.pop(run_id, time.monotonic())) * 1000
                )
                completed_tool_calls.append({
                    "call_id": run_id,
                    "name": resolved_name,
                    "args": resolved_args,
                    "result_preview": preview,
                    "citations": citations or None,
                    "rewritten": rewritten or None,
                    "elapsed_ms": tool_elapsed_ms,
                    "agent": resolved_agent,
                })
                # 回填时间线里对应的工具步骤
                step = _tool_step_by_id.get(run_id)
                if step is not None:
                    step.update({
                        "name": resolved_name,
                        "args": resolved_args,
                        "result_preview": preview,
                        "citations": citations or None,
                        "rewritten": rewritten or None,
                        "elapsed_ms": tool_elapsed_ms,
                        "agent": resolved_agent,
                    })
                yield sse_pack(
                    "tool_call_end",
                    {
                        "call_id": run_id,
                        "name": resolved_name,
                        "result_preview": preview,
                        "citations": citations,
                        "rewritten": rewritten or None,
                        "elapsed_ms": tool_elapsed_ms,
                        "agent": resolved_agent,
                    },
                )

            # ---------------- Custom event（顶层图发起合同审查等） ---------------- #
            elif etype == "on_custom_event" and ev.get("name") == "review_started":
                # enqueue_review_node 通过 adispatch_custom_event 推出；前端用它作为
                # 「打开审查 SSE 同步面板进度」的信号。
                payload = data if isinstance(data, dict) else {}
                yield sse_pack("review_started", payload)

        # 兜底：模型偶发对同一工具发出被放弃/无效的 tool_call（如缺必填参数），astream_events 会给出
        # on_tool_start 却没有对应 on_tool_end，前端那张工具卡片就会一直「检索中」转圈。流正常结束前，
        # 把所有未配对的 tool_call_start 各补一条 tool_call_end 收尾，保证不会有卡片永久挂起。
        for orphan_id in list(pending_tool_starts.keys()):
            o_name = pending_tool_names.pop(orphan_id, "tool")
            o_agent = pending_tool_agents.pop(orphan_id, "")
            o_elapsed = int(
                (time.monotonic() - pending_tool_starts.pop(orphan_id, time.monotonic())) * 1000
            )
            pending_tool_args.pop(orphan_id, None)
            step = _tool_step_by_id.get(orphan_id)
            if step is not None:
                step.update({"result_preview": "（该工具调用未完成）", "elapsed_ms": o_elapsed})
            yield sse_pack(
                "tool_call_end",
                {
                    "call_id": orphan_id,
                    "name": o_name,
                    "result_preview": "（该工具调用未完成）",
                    "citations": [],
                    "rewritten": None,
                    "elapsed_ms": o_elapsed,
                    "agent": o_agent,
                },
            )

        # HITL：图可能在 ensure_stance 处 interrupt 暂停。流末检查 checkpoint 状态，
        # 命中则把 interrupt 的 value 转成 stance_required 事件交前端，等用户选择后
        # 以 resume 重入本端点。aget_state 需要顶层图挂 checkpointer。
        try:
            snapshot = await agent.aget_state(config)
            interrupts = getattr(snapshot, "interrupts", None) or ()
            if interrupts:
                payload = getattr(interrupts[0], "value", {}) or {}
                if isinstance(payload, dict) and payload.get("type") == "party_stance_request":
                    await _persist_current_response()
                    yield sse_pack("stance_required", {
                        "contract_id": payload.get("contract_id"),
                        "options": payload.get("options") or ["甲方", "乙方", "中立"],
                    })
                    # 不发 done：前端据 stance_required 渲染选择卡片，选后 resume 重入。
                    return
        except Exception:
            logger.exception("检查 pending interrupt 失败 session=%s", session_id)

        _, _, thinking_ms = await _persist_current_response()

        yield sse_pack(
            "done",
            {
                "message_id": message_id,
                "citations": last_citations,
                "thinking_ms": thinking_ms,
            },
        )

    except asyncio.CancelledError:
        await _persist_current_response()
        raise
    except asyncio.TimeoutError:
        logger.warning("Agent stream idle for %.0fs, aborting", STREAM_IDLE_TIMEOUT_S)
        yield sse_pack(
            "error",
            {
                "message": "上游模型长时间无响应（可能触发限流），请稍后重试。",
            },
        )
        yield sse_pack("done", {"message_id": message_id, "citations": last_citations, "thinking_ms": None})
    except Exception as exc:  # pragma: no cover - best-effort surface to client
        logger.exception("stream_agent_as_sse failed")
        yield sse_pack("error", {"message": f"{type(exc).__name__}: {exc}"})
        yield sse_pack("done", {"message_id": message_id, "citations": last_citations, "thinking_ms": None})


__all__ = ["sse_pack", "stream_agent_as_sse"]
