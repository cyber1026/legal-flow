"""审查流只订阅 supervisor 已启动任务；不会自行启动 pending 合同审查。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.api.routes.contract_review import stream_review
from app.auth.models import UserPublic
from app.contracts.store import ContractRecord, ContractStore
import app.contracts.review_manager as rm


def _contract(*, party_stance: str = "未知", status: str = "pending") -> ContractRecord:
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return ContractRecord(
        id=10,
        user_id=1,
        session_id="s1",
        job_id="job1",
        filename="合同.pdf",
        mime="application/pdf",
        doc_type="pdf",
        storage_path="/tmp/contract.pdf",
        title="合同.pdf",
        status=status,
        parsed_clauses=0,
        risk_count=0,
        error=None,
        started_at=None,
        finished_at=None,
        created_at=now,
        party_stance=party_stance,
    )


def test_stream_review_pending_contract_is_not_auto_started(monkeypatch):
    monkeypatch.setattr(
        ContractStore,
        "get_owned",
        staticmethod(lambda contract_id, user_id: _contract(party_stance="甲方")),
    )

    started: list[int] = []

    async def fake_ensure_started(contract_id: int, *, force_reset: bool = False) -> None:
        started.append(contract_id)

    async def fake_inactive(contract_id: int) -> bool:
        return False

    monkeypatch.setattr(rm.contract_review_manager, "ensure_started", fake_ensure_started)
    monkeypatch.setattr(rm.contract_review_manager, "is_active", fake_inactive)

    current = UserPublic(
        id=1,
        email="user@example.com",
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )

    async def _collect() -> str:
        resp = await stream_review(10, current=current)
        chunks: list[str] = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    body = asyncio.run(_collect())

    assert "event: review_not_started" in body
    assert '"contract_id":10' in body
    assert started == []


def test_stream_review_active_unknown_stance_requests_stance(monkeypatch):
    monkeypatch.setattr(
        ContractStore,
        "get_owned",
        staticmethod(lambda contract_id, user_id: _contract()),
    )
    async def fake_active(contract_id: int) -> bool:
        return True

    monkeypatch.setattr(rm.contract_review_manager, "is_active", fake_active)

    current = UserPublic(
        id=1,
        email="user@example.com",
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )

    async def _collect() -> str:
        resp = await stream_review(10, current=current)
        chunks: list[str] = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    body = asyncio.run(_collect())

    assert "event: stance_required" in body
    assert '"contract_id":10' in body
