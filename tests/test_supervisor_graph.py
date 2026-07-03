"""supervisor 顶层图的路由 + enqueue_review 节点测试。

打桩 review_manager 与 dispatch_custom_event，验证：
- 路由函数：AIMessage.tool_calls 含 start_contract_review → "review"；否则 → "end"。
- enqueue_review_node：成功路径调用 ensure_started(force_reset=True) 且 dispatch review_started 事件；
  contract_id 为空时跳过（不调 ensure_started）；ensure_started 抛错时仍不向上传播。
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage

import app.agents.supervisor as sup
import app.contracts.review_manager as rm


# --------------------------------------------------------------------------- #
# 路由函数
# --------------------------------------------------------------------------- #

def test_route_after_supervisor_routes_review_when_tool_called():
    state = {
        "messages": [
            HumanMessage(content="审一下这份合同"),
            AIMessage(
                content="",
                tool_calls=[{"name": "start_contract_review", "args": {"reason": "用户要求"}, "id": "t1"}],
            ),
        ]
    }
    assert sup._route_after_supervisor(state) == "review"


def test_route_after_supervisor_routes_end_for_plain_reply():
    state = {
        "messages": [
            HumanMessage(content="第三条是什么"),
            AIMessage(content="第三条规定……"),
        ]
    }
    assert sup._route_after_supervisor(state) == "end"


def test_route_after_supervisor_routes_end_when_other_tool_called():
    state = {
        "messages": [
            HumanMessage(content="给我查一下合同法第 509 条"),
            AIMessage(
                content="",
                tool_calls=[{"name": "verify_law_article", "args": {}, "id": "t1"}],
            ),
        ]
    }
    assert sup._route_after_supervisor(state) == "end"


def test_route_after_supervisor_empty_messages():
    assert sup._route_after_supervisor({"messages": []}) == "end"
    assert sup._route_after_supervisor({}) == "end"


# --------------------------------------------------------------------------- #
# enqueue_review_node
# --------------------------------------------------------------------------- #

def test_enqueue_review_starts_review_and_dispatches_event(monkeypatch):
    started: list[tuple[int, bool]] = []
    dispatched: list[tuple[str, dict]] = []

    async def fake_ensure_started(contract_id: int, *, force_reset: bool = False) -> None:
        started.append((contract_id, force_reset))

    async def fake_dispatch(name: str, data: dict) -> None:
        dispatched.append((name, data))

    monkeypatch.setattr(rm.contract_review_manager, "ensure_started", fake_ensure_started)
    monkeypatch.setattr(sup, "adispatch_custom_event", fake_dispatch)

    out = asyncio.run(sup.enqueue_review_node({"contract_id": 42}))

    assert out == {}
    assert started == [(42, True)]
    assert dispatched == [("review_started", {"contract_id": 42})]


def test_enqueue_review_skips_when_contract_id_missing(monkeypatch):
    started: list[tuple[int, bool]] = []
    dispatched: list[tuple[str, dict]] = []

    async def fake_ensure_started(contract_id: int, *, force_reset: bool = False) -> None:
        started.append((contract_id, force_reset))

    async def fake_dispatch(name: str, data: dict) -> None:
        dispatched.append((name, data))

    monkeypatch.setattr(rm.contract_review_manager, "ensure_started", fake_ensure_started)
    monkeypatch.setattr(sup, "adispatch_custom_event", fake_dispatch)

    asyncio.run(sup.enqueue_review_node({"contract_id": None}))
    asyncio.run(sup.enqueue_review_node({}))  # 没有 contract_id 字段

    assert started == []
    assert dispatched == []


def test_enqueue_review_swallows_ensure_started_errors(monkeypatch):
    """ensure_started 抛错时不应让顶层图 raise（错误隔离原则同 review_clause）。"""
    dispatched: list[tuple[str, dict]] = []

    async def boom_ensure_started(contract_id: int, *, force_reset: bool = False) -> None:
        raise RuntimeError("boom")

    async def fake_dispatch(name: str, data: dict) -> None:
        dispatched.append((name, data))

    monkeypatch.setattr(rm.contract_review_manager, "ensure_started", boom_ensure_started)
    monkeypatch.setattr(sup, "adispatch_custom_event", fake_dispatch)

    out = asyncio.run(sup.enqueue_review_node({"contract_id": 7}))
    assert out == {}
    # ensure_started 失败时不再 dispatch（用户已通过工具 return_direct 收到确认了，不必额外信号）
    assert dispatched == []
