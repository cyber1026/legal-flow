"""Chat / sessions request and response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """Body for ``POST /chat``."""

    session_id: str | None = Field(
        default=None,
        description="若为空，后端会自动创建一个新会话并把 session_id 通过 SSE `session` 事件回传。",
    )
    content: str = Field(min_length=0, max_length=8000, default="")
    resume: str | None = Field(
        default=None,
        description="非空表示本次是 HITL 应答（如所选委托人立场），后端用 Command(resume=...) 恢复被 interrupt 暂停的图。",
    )
    images: list[str] | None = Field(
        default=None,
        description=(
            "可选的图像列表（最多 6 张），每个元素是 `data:image/...;base64,...` "
            "或公网可达的 URL。会作为 OpenAI 兼容的多模态 content 发送给当前 LLM。"
        ),
        max_length=6,
    )


class SessionOut(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    contract_id: int | None = None


class SessionRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class SessionCreateRequest(BaseModel):
    """可选的 POST /sessions 请求体：允许调用方指定初始标题。"""

    title: str | None = Field(default=None, max_length=120)


class MessageOut(BaseModel):
    id: int
    session_id: str
    role: str
    content: str
    citations: list[dict[str, Any]] | None = None
    tool_calls: list[dict[str, Any]] | None = None
    thinking: str | None = None
    thinking_ms: int | None = None
    reasoning: list[dict[str, Any]] | None = None
    images: list[str] | None = None
    created_at: datetime


class SessionDetail(SessionOut):
    messages: list[MessageOut]


__all__ = [
    "ChatRequest",
    "SessionOut",
    "SessionRenameRequest",
    "MessageOut",
    "SessionDetail",
]
