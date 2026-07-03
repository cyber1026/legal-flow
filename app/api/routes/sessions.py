"""Chat session CRUD: list / detail / rename / delete."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.schemas.chat import (
    MessageOut,
    SessionCreateRequest,
    SessionDetail,
    SessionOut,
    SessionRenameRequest,
)
from app.auth.deps import get_current_user
from app.auth.models import UserPublic
from app.contracts.milvus_store import delete_by_contract
from app.contracts.store import ContractStore
from app.sessions.store import SessionStore

router = APIRouter(prefix="/sessions", tags=["sessions"])
logger = logging.getLogger(__name__)


def _msg_out(m) -> MessageOut:
    return MessageOut(
        id=m.id,
        session_id=m.session_id,
        role=m.role,
        content=m.content,
        citations=m.citations,
        tool_calls=m.tool_calls,
        thinking=m.thinking,
        thinking_ms=m.thinking_ms,
        reasoning=m.reasoning,
        images=m.images,
        created_at=m.created_at,
    )


def _sess_out(s) -> SessionOut:
    return SessionOut(
        id=s.id,
        title=s.title,
        created_at=s.created_at,
        updated_at=s.updated_at,
        contract_id=s.contract_id,
    )


@router.get("", response_model=list[SessionOut])
async def list_sessions(current: UserPublic = Depends(get_current_user)) -> list[SessionOut]:
    return [_sess_out(s) for s in SessionStore.list(current.id)]


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: SessionCreateRequest | None = None,
    current: UserPublic = Depends(get_current_user),
) -> SessionOut:
    title = (body.title or "").strip() if body else ""
    s = SessionStore.create(current.id, title=title or None)
    return _sess_out(s)


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session(
    session_id: str,
    current: UserPublic = Depends(get_current_user),
) -> SessionDetail:
    s = SessionStore.get(session_id, current.id)
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    msgs = SessionStore.get_messages(session_id, current.id)
    return SessionDetail(
        id=s.id,
        title=s.title,
        created_at=s.created_at,
        updated_at=s.updated_at,
        contract_id=s.contract_id,
        messages=[_msg_out(m) for m in msgs],
    )


@router.patch("/{session_id}", response_model=SessionOut)
async def rename_session(
    session_id: str,
    payload: SessionRenameRequest,
    current: UserPublic = Depends(get_current_user),
) -> SessionOut:
    s = SessionStore.rename(session_id, current.id, payload.title.strip())
    if not s:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    return _sess_out(s)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str,
    current: UserPublic = Depends(get_current_user),
) -> None:
    sess = SessionStore.get(session_id, current.id)
    if not sess:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")

    contracts = ContractStore.list_by_session(session_id, current.id)
    for contract in contracts:
        try:
            delete_by_contract(contract.id)
        except Exception as exc:
            logger.exception("删除会话时清理合同向量失败 contract=%s", contract.id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"清理合同向量失败：{contract.filename}",
            ) from exc

        try:
            Path(contract.storage_path).unlink(missing_ok=True)
        except OSError as exc:
            logger.exception("删除会话时清理合同原文件失败 path=%s", contract.storage_path)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"清理合同原文件失败：{contract.filename}",
            ) from exc

    for contract in contracts:
        ContractStore.delete(contract.id, current.id)

    if not SessionStore.delete(session_id, current.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")

    # checkpoint 线程（thread_id == session_id）非外键关联，不随 DELETE FROM sessions 级联清理，
    # 需显式删除，否则 PG 里堆 orphan。失败不阻塞会话删除，仅记日志。
    from app.core.checkpointer import get_checkpointer

    cp = get_checkpointer()
    if cp is not None:
        try:
            await cp.adelete_thread(session_id)
        except Exception:
            logger.exception("删除会话 checkpoint 失败 session=%s", session_id)


__all__ = ["router"]
