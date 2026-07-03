"""顶层 supervisor 图挂 checkpointer 后可编译、带 thread 可执行。"""
from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver

import app.agents.supervisor as sup
from app.agents.supervisor import build_supervisor_graph


@pytest.fixture(autouse=True)
def _stub_supervisor_node(monkeypatch):
    """构图测试只验证顶层编排，避免在 CI 初始化真实 LLM。"""

    async def _fake_supervisor(state):
        """返回空状态增量，满足 LangGraph 节点协议。"""
        return {}

    monkeypatch.setattr(sup, "get_supervisor_node", lambda: _fake_supervisor)


def test_build_with_checkpointer_compiles():
    graph = build_supervisor_graph(checkpointer=MemorySaver())
    # 编译图应带 checkpointer（可执行 aget_state 需要）
    assert graph.checkpointer is not None


def test_build_without_checkpointer_ok():
    graph = build_supervisor_graph(checkpointer=None)
    assert graph is not None
