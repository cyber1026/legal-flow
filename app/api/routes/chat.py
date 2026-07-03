"""Chat route: streams a deep-agent response over SSE."""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.agents.session_locks import get_session_lock
from app.api.deps import get_supervisor_agent
from app.api.schemas.chat import ChatRequest
from app.api.sse import sse_pack, stream_agent_as_sse
from app.auth.deps import get_current_user
from app.auth.models import UserPublic
from app.contracts.context import build_contract_context
from app.contracts.store import ContractStore
from app.sessions.store import SessionStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

_HISTORY_LIMIT = 20  # cap how much history we replay into the agent context


def _new_message_id() -> str:
    return secrets.token_urlsafe(8)


@router.post("/chat")
async def chat(
    payload: ChatRequest,
    current: UserPublic = Depends(get_current_user),
):
    """Stream an assistant reply for ``payload.content``.

    Behaviour:

    1. If ``session_id`` is missing, create a new session for the current user.
    2. Persist the user message immediately so the UI can re-render history.
    3. Run the deep-agent with prior messages as context, surface SSE events
       to the client, and persist the assistant message after the stream ends.
    """
    images: list[str] = [img for img in (payload.images or []) if img]
    # 至少需要文字或一张图片。
    if not payload.content.strip() and not images:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请输入文字或上传至少一张图片",
        )

    if payload.session_id:
        sess = SessionStore.get(payload.session_id, current.id)
        if not sess:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在"
            )
    else:
        title_seed = payload.content[:40].strip() or ("[图片] " if images else "新会话")
        sess = SessionStore.create(current.id, title=title_seed)

    # 挂 checkpointer 后，agent 工作态由 checkpointer 持有（thread_id=session_id）：
    # 正常轮只传新消息，不再回放全部历史（否则 add_messages 会把历史再 append 一遍翻倍）。
    # 仅首轮（会话尚无消息）注入一行合同上下文摘要；后续轮上下文已在 checkpoint 里。
    history = SessionStore.get_messages(sess.id, current.id, limit=_HISTORY_LIMIT)
    is_first_turn = len([m for m in history if m.role in ("user", "assistant")]) == 0
    contract = ContractStore.get_by_session(sess.id, current.id)

    history_payload: list[dict] = []
    if is_first_turn and contract:
        history_payload.append(
            {"role": "assistant", "content": build_contract_context(contract)},
        )

    # resume 轮通常 content 为空（仅恢复被暂停的图）；为空则不记 user 消息。
    if payload.content.strip() or images:
        SessionStore.append_message(
            sess.id,
            "user",
            payload.content,
            images=images or None,
        )
    if sess.title in ("新会话", "") and len(history) == 0:
        SessionStore.rename(
            sess.id,
            current.id,
            payload.content[:40].strip() or ("[图片] " if images else "新会话"),
        )

    message_id = _new_message_id()
    agent = get_supervisor_agent()

    def on_done(
        answer_text: str,
        citations: list[dict],
        thinking: str | None = None,
        tool_calls: list[dict] | None = None,
        thinking_ms: int | None = None,
        reasoning: list[dict] | None = None,
    ) -> None:
        if not answer_text:
            return
        SessionStore.append_message(
            sess.id,
            "assistant",
            answer_text,
            citations=citations or None,
            thinking=thinking or None,
            tool_calls=tool_calls or None,
            thinking_ms=thinking_ms,
            reasoning=reasoning or None,
        )

    async def event_source():
        """持会话锁消费 agent 流，避免和后台总览并发写同一 checkpoint 线程。"""
        try:
            async with get_session_lock(sess.id):
                async for chunk in stream_agent_as_sse(
                    agent,
                    payload.content,
                    session_id=sess.id,
                    message_id=message_id,
                    user_id=current.id,
                    history=history_payload,
                    on_done=on_done,
                    images=images,
                    resume=payload.resume,
                    extra_state={
                        "contract_id": contract.id if contract else None,
                    },
                ):
                    yield chunk
        except Exception as exc:  # pragma: no cover
            logger.exception("Chat stream crashed")
            yield sse_pack("error", {"message": f"{type(exc).__name__}: {exc}"})

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers=headers,
    )


__all__ = ["router"]
