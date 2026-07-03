"""按 session_id 提供进程内锁的身份语义与互斥串行化测试。"""
from __future__ import annotations

import asyncio

from app.agents.session_locks import get_session_lock


def test_same_session_returns_same_lock():
    """同一会话返回同一把锁，不同会话互不影响。"""
    assert get_session_lock("s1") is get_session_lock("s1")
    assert get_session_lock("s1") is not get_session_lock("s2")


def test_session_lock_serializes_same_session():
    """同一会话的两个协程必须串行进入临界区。"""
    order: list[str] = []

    async def worker(tag: str, hold: float) -> None:
        """记录协程进入与离开锁的顺序。"""
        async with get_session_lock("s"):
            order.append(f"{tag}-start")
            await asyncio.sleep(hold)
            order.append(f"{tag}-end")

    async def main() -> None:
        """让 A 先拿锁，确保断言顺序稳定。"""
        t1 = asyncio.create_task(worker("A", 0.05))
        await asyncio.sleep(0.01)
        t2 = asyncio.create_task(worker("B", 0.0))
        await asyncio.gather(t1, t2)

    asyncio.run(main())
    assert order == ["A-start", "A-end", "B-start", "B-end"]
