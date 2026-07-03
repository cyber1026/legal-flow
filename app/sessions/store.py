"""Chat-session and message persistence (PostgreSQL).

Designed around per-user isolation: every read and write that touches sessions
or messages takes a ``user_id`` so callers can never accidentally cross-read
another user's data. Messages additionally store optional ``citations``,
``tool_calls`` and ``thinking`` blobs as JSON for later replay in the UI.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.datetime_utils import to_utc_datetime
from app.db import get_conn


def _new_session_id() -> str:
    return secrets.token_urlsafe(12)


def _loads_or_none(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class ChatSession:
    id: str
    user_id: int
    title: str
    created_at: datetime
    updated_at: datetime
    contract_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "title": self.title,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "contract_id": self.contract_id,
        }


@dataclass(slots=True)
class ChatMessage:
    id: int
    session_id: str
    role: str
    content: str
    citations: list[dict[str, Any]] | None
    tool_calls: list[dict[str, Any]] | None
    thinking: str | None
    thinking_ms: int | None
    reasoning: list[dict[str, Any]] | None
    images: list[str] | None
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "citations": self.citations,
            "tool_calls": self.tool_calls,
            "thinking": self.thinking,
            "thinking_ms": self.thinking_ms,
            "reasoning": self.reasoning,
            "images": self.images,
            "created_at": self.created_at.isoformat(),
        }


def _row_to_session(row) -> ChatSession:
    return ChatSession(
        id=row["id"],
        user_id=row["user_id"],
        title=row["title"],
        created_at=to_utc_datetime(row["created_at"]),
        updated_at=to_utc_datetime(row["updated_at"]),
        contract_id=row.get("contract_id"),
    )


# 关联会话的合同 id（一会话一合同；多条时取最新）。作为相关子查询拼进 SELECT。
_CONTRACT_ID_SUBQUERY = (
    "(SELECT c.id FROM contracts c WHERE c.session_id = sessions.id "
    "ORDER BY c.created_at DESC LIMIT 1) AS contract_id"
)


def _row_to_message(row) -> ChatMessage:
    images_raw = _loads_or_none(row.get("images_json"))
    return ChatMessage(
        id=row["id"],
        session_id=row["session_id"],
        role=row["role"],
        content=row["content"],
        citations=_loads_or_none(row["citations_json"]),
        tool_calls=_loads_or_none(row["tool_calls_json"]),
        thinking=row["thinking"],
        thinking_ms=row["thinking_ms"],
        reasoning=_loads_or_none(row.get("reasoning_json")),
        images=images_raw if isinstance(images_raw, list) else None,
        created_at=to_utc_datetime(row["created_at"]),
    )


class SessionStore:
    """All operations are per-user and forbid cross-tenant reads."""

    @staticmethod
    def create(user_id: int, title: str | None = None) -> ChatSession:
        sid = _new_session_id()
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions(id, user_id, title) VALUES(%s, %s, %s) "
                "RETURNING id, user_id, title, created_at, updated_at",
                (sid, user_id, title or "新会话"),
            )
            row = cur.fetchone()
        return _row_to_session(row)

    @staticmethod
    def get(session_id: str, user_id: int) -> ChatSession | None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, user_id, title, created_at, updated_at, "
                f"{_CONTRACT_ID_SUBQUERY} FROM sessions "
                "WHERE id = %s AND user_id = %s",
                (session_id, user_id),
            )
            row = cur.fetchone()
        return _row_to_session(row) if row else None

    @staticmethod
    def list(user_id: int, limit: int = 100) -> list[ChatSession]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, user_id, title, created_at, updated_at, "
                f"{_CONTRACT_ID_SUBQUERY} FROM sessions "
                "WHERE user_id = %s ORDER BY updated_at DESC LIMIT %s",
                (user_id, limit),
            )
            rows = cur.fetchall()
        return [_row_to_session(r) for r in rows]

    @staticmethod
    def delete(session_id: str, user_id: int) -> bool:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM sessions WHERE id = %s AND user_id = %s",
                (session_id, user_id),
            )
            return cur.rowcount > 0

    @staticmethod
    def rename(session_id: str, user_id: int, title: str) -> ChatSession | None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET title = %s, updated_at = NOW() "
                "WHERE id = %s AND user_id = %s "
                "RETURNING id, user_id, title, created_at, updated_at",
                (title, session_id, user_id),
            )
            row = cur.fetchone()
        return _row_to_session(row) if row else None

    @staticmethod
    def touch(session_id: str) -> None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET updated_at = NOW() WHERE id = %s",
                (session_id,),
            )

    @staticmethod
    def get_messages(session_id: str, user_id: int, limit: int = 200) -> list[ChatMessage]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM sessions WHERE id = %s AND user_id = %s",
                (session_id, user_id),
            )
            if not cur.fetchone():
                return []
            cur.execute(
                "SELECT id, session_id, role, content, citations_json, tool_calls_json, "
                "thinking, thinking_ms, reasoning_json, images_json, created_at "
                "FROM messages WHERE session_id = %s ORDER BY id ASC LIMIT %s",
                (session_id, limit),
            )
            rows = cur.fetchall()
        return [_row_to_message(r) for r in rows]

    @staticmethod
    def append_message(
        session_id: str,
        role: str,
        content: str,
        *,
        citations: list[dict[str, Any]] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        thinking: str | None = None,
        thinking_ms: int | None = None,
        reasoning: list[dict[str, Any]] | None = None,
        images: list[str] | None = None,
    ) -> ChatMessage:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO messages(session_id, role, content, citations_json, "
                "tool_calls_json, thinking, thinking_ms, reasoning_json, images_json) "
                "VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id, session_id, role, content, citations_json, tool_calls_json, "
                "thinking, thinking_ms, reasoning_json, images_json, created_at",
                (
                    session_id,
                    role,
                    content,
                    json.dumps(citations, ensure_ascii=False) if citations is not None else None,
                    json.dumps(tool_calls, ensure_ascii=False) if tool_calls is not None else None,
                    thinking,
                    thinking_ms,
                    json.dumps(reasoning, ensure_ascii=False) if reasoning is not None else None,
                    json.dumps(images, ensure_ascii=False) if images else None,
                ),
            )
            row = cur.fetchone()
            cur.execute(
                "UPDATE sessions SET updated_at = NOW() WHERE id = %s",
                (session_id,),
            )
        return _row_to_message(row)


__all__ = ["SessionStore", "ChatSession", "ChatMessage"]
