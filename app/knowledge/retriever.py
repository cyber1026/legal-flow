"""审查支撑知识库的通用检索器（按 collection 参数化，5 库共用）。

v1 纯向量召回：BGE-M3 向量检索 over-fetch → Python 内按 ``score × retrieval_weight`` 重排 →
按 ``contract_domain`` / ``clause_type`` 做**软过滤**（命中者优先，未命中不强删，避免漏召）→ 取 top-k。

之所以在 Python 侧做过滤/加权：``contract_domains`` / ``clause_types`` 入库时被 JSON 序列化成字符串
（Milvus dynamic field 不存 list），用 Milvus expr 对其做「数组包含」过滤很别扭；over-fetch 后在内存里
解析回 list 再过滤既简单又稳，也方便把 ``retrieval_weight`` 当作打分权重。rerank/混合召回留作后续迭代。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from app.core.config import settings
from app.knowledge.vector_store import build_kb_vector_store

logger = logging.getLogger(__name__)


@lru_cache(maxsize=None)
def _cached_vector_store(collection: str):
    """按 collection 缓存只读向量库（进程级单例，drop_old=False）。"""
    return build_kb_vector_store(collection, drop_old=False)


def _parse_list(value: Any) -> list[str]:
    """把入库时 JSON 序列化的 list 字段还原为 list[str]；已是 list 则原样返回。"""
    if isinstance(value, list):
        return [str(x) for x in value]
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except Exception:
            return [value]
    return []


class KBRetriever:
    """对单个知识库 collection 做语义检索，返回结构化 dict 列表。

    每条结果含：``display_text``（展示正文）、``embedding_text``（向量文本）、``chunk_id``、
    ``score``（COSINE 相似度）、``weight``（retrieval_weight）、``adjusted``（score×weight，排序依据）、
    ``contract_domains`` / ``clause_types``（已解析 list），以及该库特有的来源/标题字段（透传 metadata）。
    """

    def __init__(self, collection: str) -> None:
        self._collection = collection

    @property
    def _vs(self):
        return _cached_vector_store(self._collection)

    def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        fetch_k: int | None = None,
        contract_domain: str = "",
        clause_type: str = "",
    ) -> list[dict[str, Any]]:
        """语义检索 + 加权重排 + 领域/类型软过滤。

        Args:
            query: 检索查询。
            top_k: 最终返回条数，默认 ``settings.kb_retrieve_top_k``。
            fetch_k: 向量层 over-fetch 条数，默认 ``settings.kb_retrieve_fetch_k``。
            contract_domain: 可选合同领域（如「房屋/不动产」），命中者优先。
            clause_type: 可选条款类型（如「违约」），命中者优先。
        """
        query = (query or "").strip()
        if not query:
            return []

        k_final = top_k or settings.kb_retrieve_top_k
        k_fetch = max(fetch_k or settings.kb_retrieve_fetch_k, k_final)

        try:
            docs_with_scores = self._vs.similarity_search_with_score(query, k=k_fetch)
        except Exception as exc:
            logger.error("[%s] 向量检索失败: %s", self._collection, exc)
            return []

        domain = (contract_domain or "").strip()
        ctype = (clause_type or "").strip()

        results: list[dict[str, Any]] = []
        for doc, score in docs_with_scores:
            results.append(self._to_dict(doc, float(score), domain, ctype))

        # 排序：先按「领域命中」「类型命中」（软过滤——命中者上浮，未命中保留兜底），
        # 再按 adjusted = score × weight 降序。
        results.sort(
            key=lambda r: (r["_domain_match"], r["_clause_match"], r["adjusted"]),
            reverse=True,
        )
        for r in results:
            r.pop("_domain_match", None)
            r.pop("_clause_match", None)
        return results[:k_final]

    def _to_dict(
        self, doc, score: float, domain: str, ctype: str
    ) -> dict[str, Any]:
        """LangChain Document + score → 结构化结果 dict。"""
        meta = dict(doc.metadata or {})
        domains = _parse_list(meta.get("contract_domains"))
        ctypes = _parse_list(meta.get("clause_types"))

        try:
            weight = float(meta.get("retrieval_weight") or 1.0)
        except (TypeError, ValueError):
            weight = 1.0

        out: dict[str, Any] = dict(meta)  # 透传该库全部 metadata（来源/标题/条号等）
        out["contract_domains"] = domains
        out["clause_types"] = ctypes
        out["embedding_text"] = doc.page_content
        out["display_text"] = meta.get("display_text") or doc.page_content
        out["score"] = round(score, 4)
        out["weight"] = weight
        out["adjusted"] = round(score * weight, 6)
        out["_domain_match"] = bool(domain and domain in domains)
        out["_clause_match"] = bool(ctype and ctype in ctypes)
        return out


__all__ = ["KBRetriever"]
