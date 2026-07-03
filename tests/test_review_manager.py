from __future__ import annotations

import asyncio
from datetime import datetime

import app.contracts.review_manager as rm
from app.contracts.store import ContractRecord


def _contract(status: str = "pending") -> ContractRecord:
    return ContractRecord(
        id=10,
        user_id=1,
        session_id=None,
        job_id="job",
        filename="f.pdf",
        mime="application/pdf",
        doc_type="pdf",
        storage_path="/tmp/f.pdf",
        title="测试合同",
        status=status,
        parsed_clauses=0,
        risk_count=0,
        error=None,
        started_at=None,
        finished_at=None,
        created_at=datetime(2026, 1, 1),
    )


def test_review_manager_keeps_running_after_subscriber_disconnect(monkeypatch):
    gate = asyncio.Event()
    started = 0

    async def fake_stream(contract_id: int):
        nonlocal started
        started += 1
        yield {"event": "status", "data": {"status": "reviewing"}}
        await gate.wait()
        yield {"event": "done", "data": {"status": "done", "risk_count": 0}}

    monkeypatch.setattr(rm.ContractStore, "get_by_id", staticmethod(lambda cid: _contract()))
    monkeypatch.setattr(rm, "astream_review_job", fake_stream)

    async def _drive():
        manager = rm.ContractReviewJobManager(cleanup_delay_s=999)
        await manager.ensure_started(10)

        subscription = manager.subscribe(10)
        first = await asyncio.wait_for(subscription.__anext__(), timeout=1)
        assert first["event"] == "status"
        await subscription.aclose()

        state = manager._states[10]
        assert state.task is not None
        assert not state.task.done()

        gate.set()
        await asyncio.wait_for(state.task, timeout=1)

        replayed = [event async for event in manager.subscribe(10)]
        assert [event["event"] for event in replayed] == ["status", "done"]
        assert replayed[0]["data"]["seq"] == 1
        assert replayed[1]["data"]["seq"] == 2
        assert started == 1

    asyncio.run(_drive())


def test_force_reset_reruns_done_contract(monkeypatch):
    """status=done 的合同，普通 ensure_started 不重跑；force_reset=True 时重跑并清 PG。"""
    cleared: list[int] = []
    updated: list[dict] = []
    started = 0

    async def fake_stream(contract_id: int):
        nonlocal started
        started += 1
        yield {"event": "status", "data": {"status": "parsing"}}
        yield {"event": "done", "data": {"status": "done", "risk_count": 0}}

    monkeypatch.setattr(rm.ContractStore, "get_by_id", staticmethod(lambda cid: _contract("done")))
    monkeypatch.setattr(
        rm.ContractStore, "clear_review_data",
        staticmethod(lambda cid: cleared.append(cid)),
    )
    monkeypatch.setattr(
        rm.ContractStore, "update_status",
        staticmethod(lambda cid, **kw: updated.append({"contract_id": cid, **kw}) or None),
    )
    monkeypatch.setattr(rm, "astream_review_job", fake_stream)

    async def _drive():
        manager = rm.ContractReviewJobManager(cleanup_delay_s=999)

        # 普通 ensure_started：done 状态直接返回，不重跑。
        await manager.ensure_started(10)
        assert started == 0
        assert cleared == [] and updated == []

        # force_reset=True：清 PG、回退 status，并起新任务。
        await manager.ensure_started(10, force_reset=True)
        assert cleared == [10]
        assert updated and updated[0]["status"] == "pending"
        assert updated[0]["parsed_clauses"] == 0 and updated[0]["risk_count"] == 0

        state = manager._states[10]
        assert state.task is not None
        await asyncio.wait_for(state.task, timeout=1)
        assert started == 1

        # force_reset 后能正常订阅到 status / done。
        replayed = [event async for event in manager.subscribe(10)]
        kinds = [e["event"] for e in replayed]
        assert "status" in kinds and "done" in kinds

    asyncio.run(_drive())


def test_force_reset_cancels_running_task(monkeypatch):
    """force_reset 时如果有 task 在跑，先取消旧 task 再起新 task。"""
    gate = asyncio.Event()
    started: list[int] = []

    async def fake_stream(contract_id: int):
        started.append(contract_id)
        try:
            yield {"event": "status", "data": {"status": "reviewing"}}
            await gate.wait()
            yield {"event": "done", "data": {"status": "done", "risk_count": 0}}
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(
        rm.ContractStore, "get_by_id",
        staticmethod(lambda cid: _contract("reviewing")),
    )
    monkeypatch.setattr(rm.ContractStore, "clear_review_data", staticmethod(lambda cid: None))
    monkeypatch.setattr(rm.ContractStore, "update_status", staticmethod(lambda cid, **kw: None))
    monkeypatch.setattr(rm, "astream_review_job", fake_stream)

    async def _drive():
        manager = rm.ContractReviewJobManager(cleanup_delay_s=999)

        # 第一次起任务（停在 gate）。
        await manager.ensure_started(10)
        first_task = manager._states[10].task
        assert first_task is not None

        # force_reset：取消旧 task，起新 task。
        await manager.ensure_started(10, force_reset=True)
        new_task = manager._states[10].task
        assert new_task is not None and new_task is not first_task
        assert first_task.cancelled() or first_task.done()

        gate.set()
        await asyncio.wait_for(new_task, timeout=1)
        assert len(started) == 2  # 两次都进了 fake_stream

    asyncio.run(_drive())


def test_review_manager_deduplicates_running_contract_tasks(monkeypatch):
    gate = asyncio.Event()
    started = 0

    async def fake_stream(contract_id: int):
        nonlocal started
        started += 1
        yield {"event": "status", "data": {"status": "reviewing"}}
        await gate.wait()
        yield {"event": "done", "data": {"status": "done", "risk_count": 0}}

    monkeypatch.setattr(rm.ContractStore, "get_by_id", staticmethod(lambda cid: _contract()))
    monkeypatch.setattr(rm, "astream_review_job", fake_stream)

    async def _drive():
        manager = rm.ContractReviewJobManager(cleanup_delay_s=999)
        await manager.ensure_started(10)
        await manager.ensure_started(10)
        await manager.ensure_started(10)

        subscription = manager.subscribe(10)
        await asyncio.wait_for(subscription.__anext__(), timeout=1)
        await subscription.aclose()

        assert started == 1
        gate.set()
        state = manager._states[10]
        assert state.task is not None
        await asyncio.wait_for(state.task, timeout=1)

    asyncio.run(_drive())
