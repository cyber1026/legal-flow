"""LangGraph checkpointer 单例 holder。

supervisor 顶层图的 HITL（interrupt/resume）依赖 checkpointer 持久化图状态。
生产用 AsyncPostgresSaver（复用 PG，跨请求/重启稳健）；进程级单例，lifespan 建/关。

注意：连接池用 `open=False` 创建后显式 `await pool.open()`，避免 psycopg_pool
在事件循环外隐式打开的弃用警告。
"""

from __future__ import annotations

import logging
from typing import Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from app.core.config import settings

logger = logging.getLogger(__name__)

_pool: Optional[AsyncConnectionPool] = None
_checkpointer: Optional[BaseCheckpointSaver] = None


async def init_checkpointer() -> Optional[BaseCheckpointSaver]:
    """应用启动时调用一次：建连接池 + AsyncPostgresSaver + setup 建表。"""
    global _pool, _checkpointer
    if _checkpointer is not None:
        return _checkpointer
    try:
        _pool = AsyncConnectionPool(
            conninfo=settings.database_url,
            max_size=settings.checkpointer_pool_max_size,
            kwargs={"autocommit": True, "prepare_threshold": 0},
            open=False,
        )
        await _pool.open()
        saver = AsyncPostgresSaver(_pool)
        await saver.setup()
        _checkpointer = saver
        logger.info("checkpointer 初始化完成（AsyncPostgresSaver）")
    except Exception:
        # 不静默：记录后置空，supervisor 退回无 checkpointer（HITL 不可用但问答正常）。
        logger.exception("checkpointer 初始化失败，HITL 将不可用")
        _checkpointer = None
    return _checkpointer


def get_checkpointer() -> Optional[BaseCheckpointSaver]:
    """返回进程级 checkpointer 单例；未初始化或失败时为 None。"""
    return _checkpointer


async def close_checkpointer() -> None:
    """应用关闭时调用：关连接池。"""
    global _pool, _checkpointer
    if _pool is not None:
        try:
            await _pool.close()
        except Exception:
            logger.exception("关闭 checkpointer 连接池失败")
    _pool = None
    _checkpointer = None


__all__ = ["init_checkpointer", "get_checkpointer", "close_checkpointer"]
