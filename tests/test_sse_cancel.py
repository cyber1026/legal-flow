from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.api.sse import stream_agent_as_sse


def _chunk(text: str):
    return SimpleNamespace(
        content=text,
        tool_call_chunks=None,
        tool_calls=None,
        additional_kwargs={},
    )


def test_stream_agent_persists_partial_answer_on_cancel():
    saved: list[tuple] = []

    class Agent:
        async def astream_events(self, *args, **kwargs):
            yield {"event": "on_chat_model_stream", "data": {"chunk": _chunk("半截回答")}}
            raise asyncio.CancelledError

    async def on_done(*args):
        saved.append(args)

    async def _drive():
        stream = stream_agent_as_sse(
            Agent(),
            "问题",
            session_id="s1",
            message_id="m1",
            on_done=on_done,
        )
        assert "event: session" in await stream.__anext__()
        assert "event: answer_delta" in await stream.__anext__()
        try:
            await stream.__anext__()
        except asyncio.CancelledError:
            pass
        else:  # pragma: no cover - defensive assertion
            raise AssertionError("expected CancelledError")

    asyncio.run(_drive())

    assert len(saved) == 1
    assert saved[0][0] == "半截回答"


def test_stream_agent_persists_completed_answer_once():
    saved: list[tuple] = []

    class Agent:
        async def astream_events(self, *args, **kwargs):
            yield {"event": "on_chat_model_stream", "data": {"chunk": _chunk("完整")}}
            yield {"event": "on_chat_model_stream", "data": {"chunk": _chunk("回答")}}

    def on_done(*args):
        saved.append(args)

    async def _drive():
        return [
            event
            async for event in stream_agent_as_sse(
                Agent(),
                "问题",
                session_id="s1",
                message_id="m1",
                on_done=on_done,
            )
        ]

    events = asyncio.run(_drive())

    assert len(saved) == 1
    assert saved[0][0] == "完整回答"
    assert sum("event: done" in event for event in events) == 1


def test_stream_agent_filters_internal_llm_streams():
    """工具内部 LLM（query rewrite / KB summarize）的 token 不应进入最终气泡。"""
    saved: list[tuple] = []

    class Agent:
        async def astream_events(self, *args, **kwargs):
            yield {
                "event": "on_chat_model_stream",
                "tags": ["query_rewrite"],
                "data": {"chunk": _chunk("内部改写")},
            }
            yield {
                "event": "on_chat_model_stream",
                "tags": ["kb_summarize"],
                "data": {"chunk": _chunk("内部检索摘要")},
            }
            yield {"event": "on_chat_model_stream", "data": {"chunk": _chunk("最终回答")}}

    def on_done(*args):
        saved.append(args)

    async def _drive():
        return [
            event
            async for event in stream_agent_as_sse(
                Agent(),
                "问题",
                session_id="s1",
                message_id="m1",
                on_done=on_done,
            )
        ]

    events = asyncio.run(_drive())
    answer_events = [e for e in events if "event: answer_delta" in e]

    assert len(answer_events) == 1
    assert "最终回答" in answer_events[0]
    assert all("内部" not in e for e in answer_events)
    assert saved[0][0] == "最终回答"
