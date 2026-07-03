"""审查支撑知识库的通用 Milvus 向量库（一个 collection 一个库，5 库共用本模块）。

仿照 [`app/ingest/law_ingest.py`](../ingest/law_ingest.py) 与 [`app/contracts/milvus_store.py`]
(../contracts/milvus_store.py) 的写法，但**按 collection 名参数化**——不为每个库复制一份。

约定（与法库 / 合同库一致）：
- HNSW + COSINE 索引；BGE-M3 embedding（``get_embeddings`` 单例）。
- ``page_content = embedding_text``（context-rich，决定向量质量），经 ``text_field="text"`` 落到
  Milvus 的 ``text`` 列。**因此 chunk 自带的展示用 ``text`` 字段改名为 ``display_text`` 存入 metadata**，
  避免与 langchain_milvus 的 ``text`` 列同名冲突（法库侧同理改名为 ``article_text``）。
- 其余 chunk 字段全进 metadata；Milvus dynamic field 不接受 list/None，统一 ``_serialize`` 成字符串。
"""

from __future__ import annotations

import json
from typing import Any

from app.core.milvus_warnings import suppress_pymilvus_deprecation_warnings

suppress_pymilvus_deprecation_warnings()

from langchain_core.documents import Document
from langchain_milvus import Milvus
from pymilvus import MilvusClient, connections

from app.core.config import settings
from app.llm.embeddings import get_embeddings


def _index_params() -> dict:
    return {
        "index_type": "HNSW",
        "metric_type": "COSINE",
        "params": {"M": 16, "efConstruction": 200},
    }


def _search_params() -> dict:
    return {"metric_type": "COSINE", "params": {"ef": 64}}


def _ensure_orm_connection() -> None:
    """确保 pymilvus ORM 连接已建立（langchain_milvus 的 add/search/col 走 ORM Collection）。"""
    probe = MilvusClient(uri=settings.milvus_uri)
    alias = probe._using
    existing = {name for name, _ in connections.list_connections()}
    if alias not in existing:
        connections.connect(alias=alias, uri=settings.milvus_uri)


def build_kb_vector_store(collection_name: str, *, drop_old: bool = False) -> Milvus:
    """构建绑定到指定 collection 的 Milvus 向量库。

    Args:
        collection_name: 目标 collection（来自 ``KBSpec.collection``）。
        drop_old: True 时清空并重建（入库 ``--rebuild`` 用）；False 时复用/追加。

    不走 lru_cache：检索侧自己按 collection 缓存（见 retriever），入库侧每库各调一次即可。
    """
    _ensure_orm_connection()
    return Milvus(
        embedding_function=get_embeddings(),
        collection_name=collection_name,
        connection_args={"uri": settings.milvus_uri},
        index_params=_index_params(),
        search_params=_search_params(),
        primary_field="pk",
        text_field="text",
        vector_field="vector",
        enable_dynamic_field=True,
        auto_id=True,
        drop_old=drop_old,
        consistency_level="Strong",
    )


def _serialize(v: Any) -> Any:
    """Milvus dynamic field 不接受 list/None，统一转为字符串（list → JSON）。"""
    if v is None:
        return ""
    if isinstance(v, list):
        return json.dumps(v, ensure_ascii=False)
    return v


# 写入 metadata 时跳过的键：``embedding_text`` 已作 page_content；``text`` 改名 display_text 另存
# （避免与 langchain_milvus 的 text 列同名）。
_SKIP_META_KEYS = {"embedding_text", "text"}

# Milvus 单行 dynamic field（$meta，存 JSON）字节上限 65536。标准条款/标准合同的
# clause_text/risk_tips/整份合同正文可达 50–270KB，全塞进 metadata 会触发
# "dynamic field exceeds max length"。故对字符串字段做字节级截断 + 整行预算兜底。
_MAX_FIELD_BYTES = 16000     # 单个字符串字段字节上限（≈5300 个汉字，足够总结链使用）
_META_BUDGET_BYTES = 60000   # 整行 metadata 预算（留余量于 65536）
_TRUNC_MARK = "…[截断]"


def _truncate_bytes(s: str, max_bytes: int) -> str:
    """按 UTF-8 字节截断字符串（不切断多字节字符），超限才加截断标记。"""
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    return b[:max_bytes].decode("utf-8", errors="ignore") + _TRUNC_MARK


def _meta_bytes(meta: dict[str, Any]) -> int:
    return len(json.dumps(meta, ensure_ascii=False).encode("utf-8"))


def _fit_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """把单行 metadata 压到 Milvus dynamic field 上限内：先逐字段硬截断，仍超则反复砍最长字段。"""
    for k, v in meta.items():
        if isinstance(v, str):
            meta[k] = _truncate_bytes(v, _MAX_FIELD_BYTES)

    guard = 0
    while _meta_bytes(meta) > _META_BUDGET_BYTES and guard < 50:
        guard += 1
        longest_k = max(
            (k for k, v in meta.items() if isinstance(v, str)),
            key=lambda k: len(meta[k].encode("utf-8")),
            default=None,
        )
        if longest_k is None:
            break
        cur = meta[longest_k].encode("utf-8")
        if len(cur) <= 400:  # 已无可观削减空间，避免死循环
            break
        meta[longest_k] = cur[: len(cur) // 2].decode("utf-8", errors="ignore") + _TRUNC_MARK
    return meta


def chunk_to_document(chunk: dict) -> Document:
    """将一条归一化 chunk dict 转为 LangChain Document。

    - page_content = embedding_text（缺失时退回 text）
    - metadata = 其余全部字段（list/None 序列化）+ ``display_text``（原 text，供展示/总结），
      并经 ``_fit_metadata`` 压到 Milvus 单行 dynamic field 上限内。
    """
    embedding_text = chunk.get("embedding_text") or chunk.get("text") or ""
    metadata: dict[str, Any] = {
        k: _serialize(v) for k, v in chunk.items() if k not in _SKIP_META_KEYS
    }
    metadata["display_text"] = chunk.get("text") or ""
    return Document(page_content=embedding_text, metadata=_fit_metadata(metadata))


__all__ = ["build_kb_vector_store", "chunk_to_document"]
