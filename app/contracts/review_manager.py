"""进程内合同审查任务管理器。

把“执行审查”和“SSE 连接”拆开：浏览器断开连接只会移除订阅者，
不会取消正在运行的审查任务。该实现覆盖单进程部署下的切会话/刷新恢复。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.contracts.review_pipeline import astream_review_job
from app.contracts.store import ContractStore

logger = logging.getLogger(__name__)

_TERMINAL_EVENTS = {"done", "error"}


@dataclass(slots=True)
class _ReviewJobState:
    task: asyncio.Task[None] | None = None
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)
    events: list[dict[str, Any]] = field(default_factory=list)
    next_seq: int = 1
    terminal: bool = False


class ContractReviewJobManager:
    """管理合同审查后台任务与 SSE 订阅者。"""

    def __init__(self, *, cleanup_delay_s: float = 30 * 60) -> None:
        self._states: dict[int, _ReviewJobState] = {}
        self._lock = asyncio.Lock()
        self._cleanup_delay_s = cleanup_delay_s

    async def is_active(self, contract_id: int) -> bool:
        """返回当前进程内是否已有该合同的审查任务在运行。"""
        async with self._lock:
            state = self._states.get(contract_id)
            return bool(state and state.task is not None and not state.task.done())

    async def ensure_started(
        self, contract_id: int, *, force_reset: bool = False
    ) -> None:
        """确保合同审查任务已启动；同一合同同一时间只跑一个任务。

        ``force_reset=True``：用于 supervisor 在对话中触发整份审查/重审时，取消现有 task、
        清掉内存缓存与 PG 旧数据（contract_clauses / risk_items），回退合同 status 到
        ``pending``，再起新任务。已 ``done`` 的合同也会被重审
        （见 ``app/agents/supervisor.py::enqueue_review_node``）。
        """
        contract = await asyncio.to_thread(ContractStore.get_by_id, contract_id)
        if contract is None:
            raise ValueError(f"contract not found: {contract_id}")
        if contract.status == "done" and not force_reset:
            return

        old_task: asyncio.Task[None] | None = None

        async with self._lock:
            state = self._states.setdefault(contract_id, _ReviewJobState())

            if force_reset:
                # 取消旧 task；清缓存与终态标记，准备完全重跑。
                if state.task is not None and not state.task.done():
                    old_task = state.task
                    state.task.cancel()
                state.events.clear()
                state.next_seq = 1
                state.terminal = False
                state.task = None
                # PG：清旧条款 / 风险 / 回退 status 到 pending；放在锁内执行以保证「内存清空 + PG 清空」
                # 对其它 ensure_started/subscribe 是原子可见的。to_thread 释放事件循环、不会卡其它协程。
                try:
                    await asyncio.to_thread(ContractStore.clear_review_data, contract_id)
                    await asyncio.to_thread(
                        ContractStore.update_status,
                        contract_id,
                        status="pending",
                        parsed_clauses=0,
                        risk_count=0,
                        error="",
                    )
                except Exception:
                    logger.exception("force_reset 清理 PG 旧审查数据失败 contract=%s", contract_id)
            elif state.task is not None and not state.task.done():
                return
            elif state.terminal:
                # failed 合同通过再次打开 stream 触发重跑；重跑时丢弃旧失败事件。
                state.events.clear()
                state.next_seq = 1
                state.terminal = False

            state.task = asyncio.create_task(
                self._run(contract_id), name=f"contract-review-{contract_id}"
            )

        # best-effort 等老 task 收尾（放锁外，避免阻塞其它协程）；超时即放弃，老 task 在后台自然退出。
        # _publish 内部用 owner 校验过滤老 task 残余事件，不会污染新审查流。
        if old_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(old_task), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except BaseException:
                logger.exception("等待旧审查任务收尾时发生异常")

    async def subscribe(self, contract_id: int) -> AsyncIterator[dict[str, Any]]:
        """订阅合同审查事件，并先补播当前进程内缓存。"""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        async with self._lock:
            state = self._states.setdefault(contract_id, _ReviewJobState())
            replay = list(state.events)
            state.subscribers.add(queue)

        try:
            for event in replay:
                yield event
            if replay and replay[-1]["event"] in _TERMINAL_EVENTS:
                return

            while True:
                event = await queue.get()
                yield event
                if event["event"] in _TERMINAL_EVENTS:
                    return
        finally:
            async with self._lock:
                state = self._states.get(contract_id)
                if state is not None:
                    state.subscribers.discard(queue)

    async def _run(self, contract_id: int) -> None:
        own_task = asyncio.current_task()
        try:
            async for event in astream_review_job(contract_id):
                await self._publish(contract_id, event, owner=own_task)
        except asyncio.CancelledError:
            logger.info("合同审查后台任务被取消 contract=%s", contract_id)
            raise
        except Exception as exc:  # pragma: no cover - 防御性兜底
            logger.exception("合同审查后台任务异常 contract=%s", contract_id)
            await self._publish(
                contract_id,
                {"event": "error", "data": {"message": f"{type(exc).__name__}: {exc}"}},
                owner=own_task,
            )
        finally:
            async with self._lock:
                state = self._states.get(contract_id)
                # 只有当 state.task 还是自己时才清空——force_reset 替换 task 后这里就是新的，
                # 不应被老任务的 finally 误清。
                if state is not None and state.task is own_task:
                    state.task = None
                    if state.terminal:
                        asyncio.create_task(self._cleanup_later(contract_id, state))

    async def _publish(
        self,
        contract_id: int,
        raw: dict[str, Any],
        *,
        owner: asyncio.Task[None] | None = None,
    ) -> None:
        """把审查事件加入缓存并广播给订阅者。

        ``owner`` 不为空时校验当前 state.task 是否仍是自己——force_reset 之后老 task 收到
        CancelledError 前可能仍有 1~2 条 event 在飞，校验失败即丢弃，避免污染新审查事件流。
        """
        async with self._lock:
            state = self._states.get(contract_id)
            if state is None:
                return
            if owner is not None and state.task is not owner:
                return
            data = dict(raw.get("data") or {})
            data.setdefault("seq", state.next_seq)
            event = {"event": raw["event"], "data": data}
            state.next_seq += 1
            state.events.append(event)
            if event["event"] in _TERMINAL_EVENTS:
                state.terminal = True
            subscribers = list(state.subscribers)

        for queue in subscribers:
            queue.put_nowait(event)

    async def _cleanup_later(self, contract_id: int, state: _ReviewJobState) -> None:
        await asyncio.sleep(self._cleanup_delay_s)
        async with self._lock:
            cur = self._states.get(contract_id)
            if cur is state and cur.terminal and cur.task is None:
                self._states.pop(contract_id, None)

    async def cancel_all(self) -> None:
        """测试/进程退出时取消所有后台任务。"""
        async with self._lock:
            tasks = [s.task for s in self._states.values() if s.task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task


contract_review_manager = ContractReviewJobManager()

__all__ = ["ContractReviewJobManager", "contract_review_manager"]
