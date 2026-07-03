"""按 ``session_id`` 提供进程内单例 ``asyncio.Lock``。

审查后台任务会在 chat 线程（``thread_id == session_id``）上生成总览；用户追问也会写同一
checkpointer 线程。两处并发运行同一顶层图时，状态保存可能互相竞争，因此用进程内锁串行化
同一会话的图运行。本模块遵循当前单进程运行假设，与 review_manager 的进程内状态一致。
"""
from __future__ import annotations

import asyncio

# 会话锁缓存：session_id -> Lock。单事件循环线程内按需创建即可。
_locks: dict[str, asyncio.Lock] = {}


def get_session_lock(session_id: str) -> asyncio.Lock:
    """返回指定会话的进程内单例锁。"""
    lock = _locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[session_id] = lock
    return lock
