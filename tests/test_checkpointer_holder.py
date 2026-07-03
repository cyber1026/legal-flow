"""checkpointer holder 的 get/set 语义（用 MemorySaver 注入，不连 PG）。"""
from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver

import app.core.checkpointer as cp


def test_holder_set_get(monkeypatch):
    saver = MemorySaver()
    monkeypatch.setattr(cp, "_checkpointer", saver, raising=False)
    assert cp.get_checkpointer() is saver


def test_holder_default_none(monkeypatch):
    monkeypatch.setattr(cp, "_checkpointer", None, raising=False)
    assert cp.get_checkpointer() is None
