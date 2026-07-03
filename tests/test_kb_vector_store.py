"""审查支撑知识库入库映射 chunk_to_document 的单测。"""

from __future__ import annotations

import json

from app.knowledge.vector_store import chunk_to_document


def test_chunk_to_document_映射与序列化():
    """embedding_text→page_content；text→display_text；list→JSON 串；None→空串；标量透传。"""
    chunk = {
        "chunk_id": "c1",
        "kb_type": "judicial_article",
        "embedding_text": "向量文本",
        "text": "展示正文",
        "contract_domains": ["买卖/供货", "金融担保"],
        "clause_types": ["主体"],
        "retrieval_weight": 1.0,
        "neighbor_ids": [],
        "article_no": None,
    }
    doc = chunk_to_document(chunk)

    assert doc.page_content == "向量文本"               # embedding_text 进 page_content
    assert doc.metadata["display_text"] == "展示正文"     # 原 text 改名 display_text
    assert "text" not in doc.metadata                    # 不以 text 键存在（避免与 Milvus text 列冲突）
    assert "embedding_text" not in doc.metadata          # 不重复存 embedding_text
    assert json.loads(doc.metadata["contract_domains"]) == ["买卖/供货", "金融担保"]
    assert json.loads(doc.metadata["clause_types"]) == ["主体"]
    assert doc.metadata["neighbor_ids"] == "[]"          # 空 list 也序列化为 "[]"
    assert doc.metadata["article_no"] == ""              # None → ""
    assert doc.metadata["retrieval_weight"] == 1.0       # float 标量透传
    assert doc.metadata["chunk_id"] == "c1"


def test_chunk_to_document_无embedding时回退text():
    doc = chunk_to_document({"text": "只有展示", "chunk_id": "c2"})
    assert doc.page_content == "只有展示"
    assert doc.metadata["display_text"] == "只有展示"


def test_chunk_to_document_超大字段截断到Milvus上限内():
    """clause_text/risk_tips 各 50KB 汉字 → 单行 metadata 必须压到 65536 字节内（Milvus 上限）。"""
    big = "条" * 50000  # 50000 汉字 ≈ 150KB UTF-8
    chunk = {
        "chunk_id": "c1",
        "embedding_text": "向量文本",
        "text": big,
        "clause_text": big,
        "risk_tips": big,
        "contract_domains": ["运输物流"],
    }
    doc = chunk_to_document(chunk)

    import json as _json

    meta_bytes = len(_json.dumps(doc.metadata, ensure_ascii=False).encode("utf-8"))
    assert meta_bytes <= 65536                       # 不会触发 Milvus dynamic field 超限
    assert doc.metadata["clause_text"].endswith("…[截断]")  # 大字段被截断
    assert doc.metadata["contract_domains"] == _json.dumps(["运输物流"], ensure_ascii=False)  # 小字段不受影响
