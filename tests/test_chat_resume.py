"""ChatRequest.resume 字段存在且默认 None（HITL 应答通道）。

端到端 resume→Command 闭环由 tests/test_stance_hitl_e2e.py（Task 11）覆盖；
此处仅锁定 schema 契约，避免装配整个 chat 路由依赖。
"""
from __future__ import annotations

from app.api.schemas.chat import ChatRequest


def test_resume_field_defaults_none():
    req = ChatRequest(content="hi")
    assert req.resume is None


def test_resume_field_accepts_value():
    req = ChatRequest(content="", resume="甲方")
    assert req.resume == "甲方"
