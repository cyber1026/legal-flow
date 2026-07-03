"""KBRetriever 的单测：over-fetch → retrieval_weight 加权重排 → 领域/类型软过滤 → top-k。"""

from __future__ import annotations

import json

import app.knowledge.retriever as kr
from app.knowledge.retriever import KBRetriever


class _Doc:
    def __init__(self, page_content: str, metadata: dict) -> None:
        self.page_content = page_content
        self.metadata = metadata


def _meta(domains, ctypes, weight, cid):
    return {
        "chunk_id": cid,
        "display_text": f"text-{cid}",
        "contract_domains": json.dumps(domains, ensure_ascii=False),
        "clause_types": json.dumps(ctypes, ensure_ascii=False),
        "retrieval_weight": weight,
    }


def _patch_vs(monkeypatch, pairs):
    """把按 collection 缓存的向量库换成返回固定 (doc, score) 列表的假实现。"""

    class _VS:
        def similarity_search_with_score(self, query, k):
            return pairs[:k]

    monkeypatch.setattr(kr, "_cached_vector_store", lambda collection: _VS())


def test_按_retrieval_weight_加权重排(monkeypatch):
    """B 相似度更低但权重更高 → adjusted 更大 → 应排到 A 前面。"""
    a = (_Doc("emb-a", _meta(["买卖/供货"], ["违约"], 1.0, "a")), 0.50)  # adj 0.50
    b = (_Doc("emb-b", _meta(["租赁"], ["价款"], 2.0, "b")), 0.45)      # adj 0.90
    _patch_vs(monkeypatch, [a, b])

    out = KBRetriever("c").search("q", top_k=2, fetch_k=10)
    assert [r["chunk_id"] for r in out] == ["b", "a"]
    assert out[0]["adjusted"] == 0.9


def test_领域软过滤命中者上浮但不丢未命中(monkeypatch):
    """指定 contract_domain：命中领域者优先，未命中者仍保留作兜底。"""
    a = (_Doc("emb-a", _meta(["买卖/供货"], ["违约"], 1.0, "a")), 0.40)  # 命中领域，adj 0.40
    b = (_Doc("emb-b", _meta(["租赁"], ["价款"], 1.0, "b")), 0.80)      # 未命中，adj 0.80
    _patch_vs(monkeypatch, [a, b])

    out = KBRetriever("c").search("q", top_k=2, fetch_k=10, contract_domain="买卖/供货")
    assert [r["chunk_id"] for r in out] == ["a", "b"]  # a 虽分低但领域命中上浮


def test_top_k_截断与字段解析(monkeypatch):
    docs = [
        (_Doc(f"emb-{i}", _meta(["通用"], ["其他"], 1.0, str(i))), 0.9 - i * 0.1)
        for i in range(3)
    ]
    _patch_vs(monkeypatch, docs)

    out = KBRetriever("c").search("q", top_k=2, fetch_k=10)
    assert len(out) == 2
    assert out[0]["contract_domains"] == ["通用"]   # JSON 串已解析回 list
    assert out[0]["clause_types"] == ["其他"]
    assert out[0]["display_text"] == "text-0"
    assert "_domain_match" not in out[0]            # 内部排序辅助键已清理


def test_空查询返回空(monkeypatch):
    _patch_vs(monkeypatch, [])
    assert KBRetriever("c").search("   ") == []
