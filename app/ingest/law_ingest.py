"""法律知识库入库 Pipeline。

将 data/parsed_chunks/*.jsonl 中的法律 chunk 向量化后写入 Milvus law_chunks collection。

使用方式（程序化）：
    pipeline = LawIngestPipeline()
    n = pipeline.ingest_jsonl(Path("data/parsed_chunks/中华人民共和国数据安全法.jsonl"))
    n = pipeline.ingest_parsed_dir(Path("data/parsed_chunks"))
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
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
    probe = MilvusClient(uri=settings.milvus_uri)
    alias = probe._using
    existing = {name for name, _ in connections.list_connections()}
    if alias not in existing:
        connections.connect(alias=alias, uri=settings.milvus_uri)

logger = logging.getLogger(__name__)

# 批量写入 Milvus 的大小（法条文本较短，可以大一些）
_BATCH_SIZE = 256


# ---------------------------------------------------------------------------
# Law collection vector store
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_law_vector_store(drop_old: bool = False) -> Milvus:
    """获取绑定到 law_chunks collection 的 Milvus 实例（带 lru_cache 缓存）。

    第一次调用时创建 collection（如果不存在）。
    drop_old=True 时清空并重建（仅在 CLI reset 时用，不走 cache）。
    """
    _ensure_orm_connection()
    return Milvus(
        embedding_function=get_embeddings(),
        collection_name=settings.law_collection_name,
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


def build_law_vector_store(drop_old: bool = False) -> Milvus:
    """不经 cache 地构建 law vector store（drop_old 场景专用）。"""
    _ensure_orm_connection()
    return Milvus(
        embedding_function=get_embeddings(),
        collection_name=settings.law_collection_name,
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


def delete_by_law_name(law_name: str, vector_store: Milvus | None = None) -> None:
    """删除某部法律在 law_chunks 中的所有旧 chunk。"""
    name = (law_name or "").strip()
    if not name:
        return
    vs = vector_store or get_law_vector_store()
    col = vs.col
    if col is None:
        return
    safe = name.replace('"', '\\"')
    col.delete(expr=f'law_name == "{safe}"')
    col.flush()


# ---------------------------------------------------------------------------
# chunk dict → LangChain Document
# ---------------------------------------------------------------------------

def _serialize(v: Any) -> Any:
    """将 list/None 转为 Milvus dynamic field 可接受的类型。

    Milvus dynamic field 不支持 Python list，统一 JSON 序列化为字符串。
    None 改为空字符串，避免 schema 类型不一致。
    """
    if v is None:
        return ""
    if isinstance(v, list):
        return json.dumps(v, ensure_ascii=False)
    return v


def chunk_to_document(chunk: dict) -> Document:
    """将一条法律 chunk dict 转为 LangChain Document。

    - page_content = embedding_text（context-rich，决定向量质量）
    - metadata["article_text"] = text（原始条文，供 LLM prompt / 展示）
    - 其余 schema 字段全部放入 metadata，list/None 序列化处理
    """
    embedding_text = chunk.get("embedding_text") or chunk.get("text", "")
    article_text = chunk.get("text", "")

    metadata: dict[str, Any] = {
        "article_text": article_text,
        "chunk_id":      chunk.get("chunk_id", ""),
        "doc_id":        chunk.get("doc_id", ""),
        "doc_type":      chunk.get("doc_type", "law"),
        "law_name":      chunk.get("law_name", ""),
        "part":          _serialize(chunk.get("part")),
        "chapter":       _serialize(chunk.get("chapter")),
        "section":       _serialize(chunk.get("section")),
        "parent_path":   _serialize(chunk.get("parent_path")),
        "article_no":    chunk.get("article_no", ""),
        "keywords":      _serialize(chunk.get("keywords")),
        "citation_text": chunk.get("citation_text", ""),
        "status":        chunk.get("status", "effective"),
        "effective_date": chunk.get("effective_date", ""),
        "version":       chunk.get("version", ""),
    }

    return Document(page_content=embedding_text, metadata=metadata)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class LawIngestPipeline:
    """法律 chunk 向量入库 Pipeline。

    将 parsed_chunks JSONL 读取后批量写入 Milvus law_chunks collection。
    """

    def __init__(self, vector_store: Milvus | None = None) -> None:
        self._vs = vector_store  # lazy-init，避免启动时连接 Milvus

    @property
    def vector_store(self) -> Milvus:
        if self._vs is None:
            self._vs = get_law_vector_store()
        return self._vs

    # ------------------------------------------------------------------
    # 核心方法
    # ------------------------------------------------------------------

    def load_jsonl(self, path: Path) -> list[dict]:
        """从 JSONL 文件中读取所有 chunk dict。"""
        chunks = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    chunks.append(json.loads(line))
        return chunks

    def ingest_jsonl(self, jsonl_path: Path) -> int:
        """读取单个 JSONL 文件，写入 Milvus。返回写入 chunk 数量。"""
        path = Path(jsonl_path)
        if not path.exists():
            logger.error("JSONL 文件不存在：%s", path)
            return 0

        chunks = self.load_jsonl(path)
        if not chunks:
            logger.warning("空文件，跳过：%s", path)
            return 0

        law_name = str(chunks[0].get("law_name") or path.stem)
        delete_by_law_name(law_name, vector_store=self.vector_store)

        docs = [chunk_to_document(c) for c in chunks]

        # 分批写入避免单次请求过大
        total = 0
        for i in range(0, len(docs), _BATCH_SIZE):
            batch = docs[i: i + _BATCH_SIZE]
            self.vector_store.add_documents(batch)
            total += len(batch)
            logger.debug("已写入 %d / %d", total, len(docs))

        logger.info("入库完成：%s → %d 条", path.name, total)
        return total

    def ingest_parsed_dir(
        self,
        parsed_dir: Path | None = None,
        *,
        progress_callback: Any = None,
    ) -> dict[str, int]:
        """遍历 parsed_chunks 目录，批量写入所有 JSONL。

        Args:
            parsed_dir: JSONL 目录，默认读取 settings.law_parsed_dir
            progress_callback: 可选回调 callback(filename, n_chunks)，用于实时更新 job 状态

        Returns:
            {filename: chunk_count}，失败文件 count=-1
        """
        if parsed_dir is None:
            parsed_dir = Path(settings.law_parsed_dir)
        parsed_dir = Path(parsed_dir)

        files = sorted(parsed_dir.glob("*.jsonl"))
        if not files:
            logger.warning("在 %s 下未找到任何 .jsonl 文件", parsed_dir)
            return {}

        results: dict[str, int] = {}
        for f in files:
            try:
                n = self.ingest_jsonl(f)
                results[f.name] = n
                if progress_callback:
                    progress_callback(f.name, n)
            except Exception as exc:
                logger.error("入库失败 %s: %s", f.name, exc)
                results[f.name] = -1

        return results
