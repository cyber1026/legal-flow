"""合同条款向量库（Milvus contract_chunks collection）。

仿照 [`app/ingest/law_ingest.py`](../../ingest/law_ingest.py) 的写法搭建独立 collection，
和 law_chunks 完全隔离，schema 字段不同，但共享 BGE-M3 embedding。

设计要点：
- 一个 chunk = 合同里的一条条款
- page_content 写 embedding_text（含「合同标题/章节/条款编号/正文」上下文）
- 其余元数据（contract_id、user_id、bbox、page_no 等）放 metadata
- 删除合同时通过 `contract_id == ?` 的 expr 整批清空
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from app.core.milvus_warnings import suppress_pymilvus_deprecation_warnings

suppress_pymilvus_deprecation_warnings()

from langchain_core.documents import Document
from langchain_milvus import Milvus
from pymilvus import MilvusClient, connections

from app.core.config import settings
from app.llm.embeddings import get_embeddings

logger = logging.getLogger(__name__)

_BATCH_SIZE = 128


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


@lru_cache(maxsize=1)
def get_contract_vector_store() -> Milvus:
    """获取绑定到 contract_chunks collection 的 Milvus 实例（lru_cache 进程单例）。"""
    _ensure_orm_connection()
    return Milvus(
        embedding_function=get_embeddings(),
        collection_name=settings.contract_collection_name,
        connection_args={"uri": settings.milvus_uri},
        index_params=_index_params(),
        search_params=_search_params(),
        primary_field="pk",
        text_field="text",
        vector_field="vector",
        enable_dynamic_field=True,
        auto_id=True,
        drop_old=False,
        consistency_level="Strong",
    )


def _serialize(v: Any) -> Any:
    """Milvus dynamic field 不接受 list/None，统一转为字符串。"""
    if v is None:
        return ""
    if isinstance(v, list):
        return json.dumps(v, ensure_ascii=False)
    return v


def build_embedding_text(
    *,
    contract_title: str,
    section_path: str,
    clause_no: str,
    clause_title: str,
    clause_text: str,
) -> str:
    """构造写入 Milvus 的 embedding 文本。

    模板与 [`docs/module/chunking_schema.md`](../../../docs/module/chunking_schema.md) 中
    法律 chunk 的思路一致：把层级上下文塞进 embedding，提升检索召回。
    """
    head = f"条款：{clause_no} {clause_title}".strip()
    parts = [f"合同：{contract_title}".strip(), f"章节：{section_path}".strip(), head, clause_text]
    return "\n\n".join(p for p in parts if p)


def clause_to_document(
    *,
    chunk_id: str,
    contract_id: int,
    user_id: int,
    contract_title: str,
    section_path: str,
    clause_no: str,
    clause_title: str,
    clause_text: str,
    page_no: int | None,
    bbox: list[float] | list[list[float]] | None,
) -> Document:
    """将一条合同条款转为 LangChain Document（用于 vector_store.add_documents）。"""
    embedding_text = build_embedding_text(
        contract_title=contract_title,
        section_path=section_path,
        clause_no=clause_no,
        clause_title=clause_title,
        clause_text=clause_text,
    )
    metadata: dict[str, Any] = {
        "chunk_id": chunk_id,
        "contract_id": contract_id,
        "user_id": user_id,
        "doc_type": "contract",
        "status": "active",
        "contract_title": contract_title,
        "section_path": section_path,
        "clause_no": clause_no,
        "title": clause_title,
        "clause_text": clause_text,
        "page_no": page_no if page_no is not None else -1,
        "bbox": _serialize(bbox),
    }
    return Document(page_content=embedding_text, metadata=metadata)


def add_clauses(documents: list[Document]) -> int:
    """批量写入合同条款 Document，返回写入数量。"""
    if not documents:
        return 0
    vs = get_contract_vector_store()
    total = 0
    for i in range(0, len(documents), _BATCH_SIZE):
        batch = documents[i : i + _BATCH_SIZE]
        vs.add_documents(batch)
        total += len(batch)
    logger.info("合同条款入向量库完成：%d 条", total)
    return total


def delete_by_contract(contract_id: int) -> None:
    """删除某合同在 contract_chunks 中的所有 chunk（用于 DELETE 接口）。"""
    vs = get_contract_vector_store()
    col = vs.col
    if col is None:
        return
    try:
        col.delete(expr=f"contract_id == {int(contract_id)}")
        col.flush()
    except Exception:
        logger.exception("删除合同向量失败 contract_id=%s", contract_id)


__all__ = [
    "get_contract_vector_store",
    "build_embedding_text",
    "clause_to_document",
    "add_clauses",
    "delete_by_contract",
]
