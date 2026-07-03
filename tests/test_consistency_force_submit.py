"""一致性审查 agent 漏调 submit_consistency_review 时的「强制补提交 + 失败兜底」行为测试。

与条款级 review_agent 同一根因/同一套路：DeepSeek 在「无一致性问题」时常用文本收尾、漏调工具。
- 强制补提交成功 → 救回结构化结果（不再静默当无风险）。
- 强制补提交仍失败 → 抛 ConsistencyReviewNotSubmittedError，由上层标 failed。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.contracts import consistency_agent
from app.contracts.consistency_agent import (
    ConsistencyReviewNotSubmittedError,
    areview_consistency,
)


def _payload_with_facts():
    """带可比对事实的输入（否则会走「无事实」早退，不触达 agent）。"""
    return {
        "consistency_facts": [
            {"clause_id": "c1", "key": "甲方名称", "value_text": "●●公司"},
            {"clause_id": "c2", "key": "甲方名称", "value_text": "××公司"},
        ],
        "opinions": [],
        "clause_risk_assessments": [],
    }


def _submission_payload():
    """一份合法的 submit_consistency_review 参数（无意见、无风险）。"""
    return {
        "has_opinion": False,
        "opinions": [],
        "risk_assessment": {
            "risk_level": "none",
            "rationale": "经横向比对未发现跨条款冲突",
            "affected_party": "不适用",
            "confidence": 0.9,
        },
        "note": "补提交：无一致性问题",
    }


class _FakeAgent:
    """模拟跑完却只用文本收尾、从不调用 submit_consistency_review 的一致性 agent。"""

    async def ainvoke(self, _inputs, config=None):
        return {
            "messages": [
                HumanMessage(content="（一致性审查 prompt）"),
                AIMessage(content="未发现一致性问题。"),
            ]
        }


class _FakeForced:
    """模拟强制补提交模型：按构造时给定的 tool_calls 返回。"""

    def __init__(self, tool_calls):
        self._tool_calls = tool_calls

    async def ainvoke(self, _messages, config=None):
        return SimpleNamespace(content="", tool_calls=self._tool_calls)


def test_force_submit_salvages_when_agent_skips_submit(monkeypatch):
    """漏调 submit_consistency_review 时，强制补提交成功应救回结构化结果。"""
    monkeypatch.setattr(consistency_agent, "get_consistency_agent", lambda: _FakeAgent())
    monkeypatch.setattr(
        consistency_agent,
        "_build_force_submit_model",
        lambda: _FakeForced(
            [{"name": "submit_consistency_review", "args": _submission_payload()}]
        ),
    )

    review = asyncio.run(areview_consistency(_payload_with_facts()))

    assert review.has_opinion is False
    assert review.risk_assessment.risk_level == "none"
    # 关键：rationale 是补提交救回的真实判断，而非旧的静默兜底文案。
    assert review.risk_assessment.rationale != "一致性审查 Agent 未提交结构化结果"


def test_raises_when_force_submit_also_fails(monkeypatch):
    """强制补提交仍拿不到 tool call 时，必须抛错（走失败路径），绝不静默无风险。"""
    monkeypatch.setattr(consistency_agent, "get_consistency_agent", lambda: _FakeAgent())
    monkeypatch.setattr(
        consistency_agent,
        "_build_force_submit_model",
        lambda: _FakeForced([]),  # 没有任何 tool call
    )

    with pytest.raises(ConsistencyReviewNotSubmittedError):
        asyncio.run(areview_consistency(_payload_with_facts()))


def test_no_facts_short_circuits_without_calling_agent(monkeypatch):
    """无可比对事实时直接早退为 none，不应触达 agent（保持原行为）。"""

    def _boom():
        raise AssertionError("无事实时不应构建 agent")

    monkeypatch.setattr(consistency_agent, "get_consistency_agent", _boom)

    review = asyncio.run(areview_consistency({"consistency_facts": []}))
    assert review.risk_assessment.risk_level == "none"
    assert review.has_opinion is False
