"""review agent 漏调 submit_review 时的「强制补提交 + 失败兜底」行为测试。

覆盖根因：DeepSeek 等模型在「无意见」条款上常直接用文本收尾、不调 submit_review。
- 强制补提交成功 → 救回结构化结果，正常 yield result（不再静默当无风险）。
- 强制补提交仍失败 → 抛 ClauseReviewNotSubmittedError，由上层标 failed。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.contracts import review_agent
from app.contracts.review_agent import (
    ClauseReviewNotSubmittedError,
    areview_clause_events,
)


def _submission_payload():
    """一份合法的 submit_review 参数（无意见、无风险）。"""
    return {
        "has_opinion": False,
        "opinions": [],
        "risk_assessment": {
            "risk_level": "none",
            "rationale": "条款为标准保密义务，未发现不利安排",
            "affected_party": "不适用",
            "confidence": 0.9,
        },
        "consistency_facts": [],
        "note": "补提交：经分析无显著风险",
    }


class _FakeAgent:
    """模拟一个跑完整轮却从不调用 submit_review、只用文本收尾的 review agent。"""

    async def astream_events(self, _inputs, version=None, config=None):
        # 一段思考 + 一句文本结论，但没有任何 submit_review 工具调用。
        yield {"event": "on_chat_model_stream", "data": {"chunk": SimpleNamespace(content="分析中…")}}
        yield {"event": "on_chat_model_end", "data": {"output": SimpleNamespace(content="未发现显著风险。")}}


class _FakeForced:
    """模拟强制补提交模型：按构造时给定的 tool_calls 返回，并记录收到的 messages。"""

    def __init__(self, tool_calls):
        self._tool_calls = tool_calls
        self.seen_messages = None

    async def ainvoke(self, messages, config=None):
        self.seen_messages = messages
        return SimpleNamespace(content="", tool_calls=self._tool_calls)


async def _collect(clause_text="甲乙双方应对业务中知悉的对方信息保密。"):
    events = []
    async for ev in areview_clause_events(
        contract_title="业务委托合同",
        section_path="",
        clause_no="第四条",
        clause_text=clause_text,
    ):
        events.append(ev)
    return events


def test_force_submit_salvages_when_agent_skips_submit(monkeypatch):
    """漏调 submit_review 时，强制补提交成功应救回结构化结果。"""
    monkeypatch.setattr(review_agent, "get_review_agent", lambda: _FakeAgent())
    monkeypatch.setattr(
        review_agent,
        "_build_force_submit_model",
        lambda: _FakeForced([{"name": "submit_review", "args": _submission_payload()}]),
    )

    events = asyncio.run(_collect())

    results = [e for e in events if e["type"] == "result"]
    assert len(results) == 1
    review = results[0]["review"]
    assert review.has_opinion is False
    assert review.risk_assessment.risk_level == "none"
    # 关键：rationale 是补提交救回的真实判断，而非旧的静默兜底文案。
    assert review.risk_assessment.rationale != "Agent 未提交审查结果"


class _FakeAgentThinking:
    """跑完整轮、思考里判了 high、但只用文本收尾不调 submit_review 的 review agent。"""

    async def astream_events(self, _inputs, version=None, config=None):
        yield {"event": "on_chat_model_stream", "data": {"chunk": SimpleNamespace(content="分析中…")}}
        # on_chat_model_end 的 output 同时带 thinking 块（结论 high）与正文。
        yield {
            "event": "on_chat_model_end",
            "data": {
                "output": SimpleNamespace(
                    content=[
                        {"type": "thinking", "text": "甲方名称缺失，风险很大，我给 high 比较合适。"},
                        {"type": "text", "text": "本条款存在主体不明确的问题。"},
                    ]
                )
            },
        }


def test_force_submit_carries_thinking_conclusion(monkeypatch):
    """补提交必须把模型 thinking 里的风险结论（high）回传，避免重新裸判导致降级。"""
    forced = _FakeForced([{"name": "submit_review", "args": _submission_payload()}])
    monkeypatch.setattr(review_agent, "get_review_agent", lambda: _FakeAgentThinking())
    monkeypatch.setattr(review_agent, "_build_force_submit_model", lambda: forced)

    asyncio.run(_collect())

    # 回传给补提交模型的消息里应包含 thinking 结论原文，且指令要求沿用结论、不得降级。
    blob = "\n".join(str(getattr(m, "content", m)) for m in forced.seen_messages)
    assert "我给 high 比较合适" in blob
    assert "本条款存在主体不明确的问题" in blob
    assert "不要重新评估" in blob or "降级" in blob


def test_raises_when_force_submit_also_fails(monkeypatch):
    """强制补提交仍拿不到 submit_review tool call 时，必须抛错（走失败路径），绝不静默无风险。"""
    monkeypatch.setattr(review_agent, "get_review_agent", lambda: _FakeAgent())
    monkeypatch.setattr(
        review_agent,
        "_build_force_submit_model",
        lambda: _FakeForced([]),  # 没有任何 tool call
    )

    with pytest.raises(ClauseReviewNotSubmittedError):
        asyncio.run(_collect())
