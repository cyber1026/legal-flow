"""立场 HITL 闭环：未知立场 interrupt → Command(resume) → 落库或取消。

纯图层测试，用 MemorySaver 作 checkpointer，绕过 chat 路由和后台审查节点。
"""
from __future__ import annotations

import asyncio

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

import app.agents.supervisor as sup


def _build_stance_only_graph():
    """构建只包含立场确认节点的测试图，聚焦验证 HITL resume 行为。"""
    graph = StateGraph(sup.SupervisorState)
    graph.add_node("ensure_stance", sup.ensure_stance_node)
    graph.add_edge(START, "ensure_stance")
    graph.add_edge("ensure_stance", END)
    return graph.compile(checkpointer=MemorySaver())


def test_interrupt_then_resume_persists(monkeypatch):
    saved: dict[int, str] = {}
    # contracts 表里立场未知 → 触发 interrupt
    monkeypatch.setattr(sup, "_load_party_stance", lambda cid: "未知")
    # 落库桩
    from app.contracts.store import ContractStore
    monkeypatch.setattr(
        ContractStore, "update_party_stance",
        staticmethod(lambda cid, s: saved.update({cid: s})),
    )

    graph = _build_stance_only_graph()
    cfg = {"configurable": {"thread_id": "s1"}}

    async def _drive():
        async for _ in graph.astream(
            {"messages": [], "contract_id": 10}, config=cfg, stream_mode="values"
        ):
            pass
        snap1 = await graph.aget_state(cfg)
        async for _ in graph.astream(
            Command(resume="甲方"), config=cfg, stream_mode="values"
        ):
            pass
        snap2 = await graph.aget_state(cfg)
        return snap1, snap2

    snap1, snap2 = asyncio.run(_drive())
    assert snap1.interrupts  # 第一次：立场未知，暂停
    assert saved.get(10) == "甲方"  # resume 后落库
    assert not snap2.interrupts  # 已恢复完成


def test_interrupt_then_cancel_marks_review_cancelled(monkeypatch):
    """用户在立场弹窗里取消：resume 取消哨兵 → 注入「取消审查」HumanMessage；不落库立场。"""
    saved: dict[int, str] = {}
    monkeypatch.setattr(sup, "_load_party_stance", lambda cid: "未知")
    from app.contracts.store import ContractStore
    monkeypatch.setattr(
        ContractStore, "update_party_stance",
        staticmethod(lambda cid, s: saved.update({cid: s})),
    )

    graph = _build_stance_only_graph()
    cfg = {"configurable": {"thread_id": "s2"}}

    async def _drive():
        async for _ in graph.astream(
            {"messages": [], "contract_id": 10}, config=cfg, stream_mode="values"
        ):
            pass
        async for _ in graph.astream(
            Command(resume=sup.CANCEL_REVIEW_SENTINEL), config=cfg, stream_mode="values"
        ):
            pass
        return await graph.aget_state(cfg)

    snap = asyncio.run(_drive())
    assert not snap.interrupts          # 取消哨兵已解除暂停
    assert saved == {}                  # 未落库立场
    assert snap.values.get("review_cancelled") is True
    # 「取消审查」作为真实用户消息进入图状态，交由上层图继续路由。
    msgs = snap.values.get("messages") or []
    assert any(
        getattr(m, "type", "") == "human" and (getattr(m, "content", "") or "") == "取消审查"
        for m in msgs
    )
