"""ensure_stance 节点：立场未知 → interrupt。"""
from __future__ import annotations

import asyncio

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import MemorySaver

import app.agents.supervisor as sup


def _build_stance_only_graph():
    """构建只包含立场确认节点的测试图，避免触发真实 LLM 和后台审查链路。"""
    graph = StateGraph(sup.SupervisorState)
    graph.add_node("ensure_stance", sup.ensure_stance_node)
    graph.add_edge(START, "ensure_stance")
    graph.add_edge("ensure_stance", END)
    return graph.compile(checkpointer=MemorySaver())


def test_unknown_stance_interrupts(monkeypatch):
    # contract 立场未知
    monkeypatch.setattr(sup, "_load_party_stance", lambda cid: "未知")

    graph = _build_stance_only_graph()
    cfg = {"configurable": {"thread_id": "s1"}}

    async def _drive():
        async for _ in graph.astream(
            {"messages": [], "contract_id": 10, "party_stance": "未知"},
            config=cfg, stream_mode="values",
        ):
            pass
        return await graph.aget_state(cfg)

    snap = asyncio.run(_drive())
    assert snap.interrupts  # 命中 interrupt
