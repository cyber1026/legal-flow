"""删会话时调用 checkpointer.adelete_thread（thread_id == session_id）。"""
from __future__ import annotations

import asyncio

import app.api.routes.sessions as sess_routes


class _FakeCP:
    def __init__(self):
        self.deleted: list[str] = []

    async def adelete_thread(self, tid):
        self.deleted.append(tid)


def test_delete_session_clears_checkpoint(monkeypatch):
    fake = _FakeCP()
    # 会话存在、无关联合同、删除成功
    monkeypatch.setattr(sess_routes.SessionStore, "get", staticmethod(lambda sid, uid: object()))
    monkeypatch.setattr(
        sess_routes.ContractStore, "list_by_session", staticmethod(lambda sid, uid: [])
    )
    monkeypatch.setattr(sess_routes.SessionStore, "delete", staticmethod(lambda sid, uid: True))
    monkeypatch.setattr("app.core.checkpointer.get_checkpointer", lambda: fake)

    class U:  # 最小 user
        id = 1

    asyncio.run(sess_routes.delete_session("s1", current=U()))
    assert fake.deleted == ["s1"]


def test_delete_session_no_checkpointer_ok(monkeypatch):
    # checkpointer 未就绪（None）时不应报错
    monkeypatch.setattr(sess_routes.SessionStore, "get", staticmethod(lambda sid, uid: object()))
    monkeypatch.setattr(
        sess_routes.ContractStore, "list_by_session", staticmethod(lambda sid, uid: [])
    )
    monkeypatch.setattr(sess_routes.SessionStore, "delete", staticmethod(lambda sid, uid: True))
    monkeypatch.setattr("app.core.checkpointer.get_checkpointer", lambda: None)

    class U:
        id = 1

    asyncio.run(sess_routes.delete_session("s1", current=U()))
