"""法律知识库检索器。

从 Milvus law_chunks collection 做语义检索，返回带完整 schema 元数据的结构化结果。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from app.core.milvus_warnings import suppress_pymilvus_deprecation_warnings

suppress_pymilvus_deprecation_warnings()

from app.core.config import settings
from app.ingest.law_ingest import get_law_vector_store

logger = logging.getLogger(__name__)


def _norm_law_name(name: str) -> str:
    """归一法名用于库内存在性比对：去掉「中华人民共和国」前缀与书名号、空白。"""
    return (name or "").replace("中华人民共和国", "").replace("《", "").replace("》", "").strip()


@lru_cache(maxsize=1)
def known_law_names() -> frozenset[str]:
    """法库中实际收录的全部法律名（去重、进程级缓存）。

    供「请求的法律是否在库」判断用。法库重灌后若需刷新，调 ``known_law_names.cache_clear()``。
    """
    try:
        col = get_law_vector_store().col
        if col is None:
            return frozenset()
        rows = col.query(expr='law_name != ""', output_fields=["law_name"], limit=16384)
    except Exception:
        logger.exception("查询法库现有法名失败")
        return frozenset()
    return frozenset(r.get("law_name", "") for r in rows if r.get("law_name"))


class LawRetriever:
    """语义检索法律条文，返回结构化 dict list。

    每条结果包含：
        - article_text: 原始条文正文（供 LLM prompt / 展示）
        - embedding_text: 向量化文本（即 Milvus text 字段）
        - citation_text: 标准引用格式，如《数据安全法》第二十一条
        - law_name, article_no, chapter, part, section
        - parent_path: 反序列化为 list
        - chunk_id, doc_id, doc_type, status, effective_date, version
        - score: 相似度得分（COSINE）
    """

    def __init__(self, k: int | None = None) -> None:
        self._k = k or settings.top_k
        self._vs = None  # lazy-init

    @property
    def _vector_store(self):
        if self._vs is None:
            self._vs = get_law_vector_store()
        return self._vs

    # ------------------------------------------------------------------
    # 核心检索方法
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        k: int | None = None,
        law_name: str | None = None,
        score_threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """语义检索法律条文。

        Args:
            query: 检索查询（自然语言或法条关键词）
            k: 返回条数，默认用 settings.top_k
            law_name: 可选，过滤指定法律（精确匹配 law_name 字段）
            score_threshold: 可选，过滤低分结果（0~1，越高越严格）

        Returns:
            list of dict，按相似度降序排列
        """
        top_k = k or self._k

        search_kwargs: dict[str, Any] = {"k": top_k}
        if score_threshold is not None:
            search_kwargs["score_threshold"] = score_threshold
        # langchain_milvus 支持通过 expr 做 metadata filter；转义引号避免法名含 " 时表达式损坏
        if law_name:
            escaped = law_name.replace('"', '\\"')
            search_kwargs["expr"] = f'law_name == "{escaped}"'

        # 单次检索：expr / score_threshold 一并下推，避免对带 law_name 的查询重复打两次 Milvus
        try:
            docs_with_scores = self._vector_store.similarity_search_with_score(
                query, **search_kwargs
            )
        except Exception as exc:
            logger.error("法律检索失败: %s", exc)
            return []

        return [self._to_dict(doc, score) for doc, score in docs_with_scores]

    def fetch_article(
        self,
        article_no: str,
        law_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """通过 article_no（可选 law_name）精确查询条文，不做向量检索。

        适用场景：用户直接点名"第三十条""第577条"等，精确命中优先于语义相似度。

        Args:
            article_no: 条文号，如"第三十条"（中文汉字形式）
            law_name:   可选，限定在某部法律内（空时跨法律搜索）

        Returns:
            匹配的条文列表，通常 0-1 条
        """
        col = self._vector_store.col
        if col is None:
            return []

        expr_parts: list[str] = []
        if law_name:
            expr_parts.append(f'law_name == "{law_name.replace(chr(34), chr(92)+chr(34))}"')
        if article_no:
            expr_parts.append(f'article_no == "{article_no.replace(chr(34), chr(92)+chr(34))}"')

        if not expr_parts:
            return []

        _FIELDS = [
            "article_text", "text", "embedding_text", "citation_text",
            "law_name", "part", "chapter", "section", "article_no",
            "chunk_id", "doc_id", "doc_type", "status",
            "effective_date", "version", "parent_path", "keywords",
        ]
        try:
            rows = col.query(
                expr=" and ".join(expr_parts),
                output_fields=_FIELDS,
            )
        except Exception as exc:
            logger.error("精确条文查询失败: %s", exc)
            return []

        return [self._row_to_dict(row) for row in rows]

    def find_law_in_kb(self, law_name: str) -> str | None:
        """把请求的法名解析到库内真实法名（归一后精确匹配，兼容「中华人民共和国」简称/全称）。

        命中返回库内规范法名；**库内根本没有这部法律时返回 None**——上层据此提示换法名，
        而不是丢掉 law_name 去跨法律堆同号条文。
        """
        req = _norm_law_name(law_name)
        if not req:
            return None
        for actual in known_law_names():
            if _norm_law_name(actual) == req:
                return actual
        return None

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _row_to_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        """将 pymilvus col.query() 返回的原始行转为结构化 dict（无 score）。"""
        parent_path = row.get("parent_path", "")
        if isinstance(parent_path, str) and parent_path:
            try:
                parent_path = json.loads(parent_path)
            except Exception:
                parent_path = [parent_path]

        keywords = row.get("keywords", "")
        if isinstance(keywords, str) and keywords:
            try:
                keywords = json.loads(keywords)
            except Exception:
                keywords = []

        article_text = row.get("article_text") or row.get("text", "")
        embedding_text = row.get("embedding_text") or row.get("text", "")

        return {
            "article_text":   article_text,
            "embedding_text": embedding_text,
            "citation_text":  row.get("citation_text", ""),
            "law_name":       row.get("law_name", ""),
            "part":           row.get("part", "") or None,
            "chapter":        row.get("chapter", "") or None,
            "section":        row.get("section", "") or None,
            "parent_path":    parent_path,
            "article_no":     row.get("article_no", ""),
            "chunk_id":       row.get("chunk_id", ""),
            "doc_id":         row.get("doc_id", ""),
            "doc_type":       row.get("doc_type", "law"),
            "keywords":       keywords,
            "status":         row.get("status", "effective"),
            "effective_date": row.get("effective_date", ""),
            "version":        row.get("version", ""),
            "score":          1.0,  # 精确匹配视为满分
        }

    def _to_dict(self, doc, score: float) -> dict[str, Any]:
        """将 LangChain Document + score 转为结构化 dict。"""
        meta = doc.metadata

        # parent_path / keywords 存储时 JSON 序列化，还原为 list
        parent_path = meta.get("parent_path", "")
        if isinstance(parent_path, str) and parent_path:
            try:
                parent_path = json.loads(parent_path)
            except Exception:
                parent_path = [parent_path]

        keywords = meta.get("keywords", "")
        if isinstance(keywords, str) and keywords:
            try:
                keywords = json.loads(keywords)
            except Exception:
                keywords = []

        return {
            # 核心内容
            "article_text":   meta.get("article_text", ""),
            "embedding_text": doc.page_content,
            "citation_text":  meta.get("citation_text", ""),
            # 法律层级
            "law_name":   meta.get("law_name", ""),
            "part":       meta.get("part", "") or None,
            "chapter":    meta.get("chapter", "") or None,
            "section":    meta.get("section", "") or None,
            "parent_path": parent_path,
            "article_no": meta.get("article_no", ""),
            # 系统字段
            "chunk_id":      meta.get("chunk_id", ""),
            "doc_id":        meta.get("doc_id", ""),
            "doc_type":      meta.get("doc_type", "law"),
            "keywords":      keywords,
            "status":        meta.get("status", "effective"),
            "effective_date": meta.get("effective_date", ""),
            "version":       meta.get("version", ""),
            # 检索得分
            "score": round(float(score), 4),
        }
