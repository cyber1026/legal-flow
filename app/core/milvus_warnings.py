"""Milvus 兼容层 warning 过滤工具。"""

from __future__ import annotations

import warnings


def suppress_pymilvus_deprecation_warnings() -> None:
    """屏蔽 PyMilvus ORM 兼容层的过时告警。

    当前 `langchain_milvus` 仍通过 PyMilvus ORM `Collection` 执行部分建库、索引和查询逻辑。
    这些告警不是本项目代码错误，但会在长流程入库/评测日志中大量刷屏；待上游完全迁移
    `MilvusClient` 后，可移除此过滤。
    """
    try:
        from pymilvus import PyMilvusDeprecationWarning
    except Exception:
        return
    warnings.filterwarnings("ignore", category=PyMilvusDeprecationWarning)
