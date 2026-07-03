"""整份标准合同逐字全文回取工具的单测（磁盘 hydrate + 定位/截断/未命中）。"""

from __future__ import annotations

import json

import app.knowledge.fulltext as ft
from app.knowledge.registry import KB_BY_KEY


class _FakeRetriever:
    """返回固定命中（含 chunk_id / 标题 / 来源）的假检索器。"""

    def __init__(self, collection):
        pass

    def search(self, q, *, top_k=1, **kw):
        return [
            {
                "chunk_id": "k1",
                "contract_title": "北京市住房租赁合同",
                "source_url": "http://example.com/c1",
            }
        ]


def test_回取整份全文(monkeypatch):
    monkeypatch.setattr(ft, "KBRetriever", _FakeRetriever)
    monkeypatch.setattr(ft, "get_fulltext_by_chunk_id", lambda kb, cid: "合同正文" * 50 if cid == "k1" else None)

    tool = ft.make_fulltext_tools()[0]
    content, art = tool.func(title_or_query="住房租赁")

    assert "北京市住房租赁合同" in content
    assert "合同正文" in content
    assert art["chunk_id"] == "k1"
    assert art["truncated"] is False
    assert art["matched"] == "北京市住房租赁合同"


def test_超长全文按上限截断(monkeypatch):
    monkeypatch.setattr(ft, "KBRetriever", _FakeRetriever)
    monkeypatch.setattr(ft.settings, "kb_fulltext_max_chars", 50)
    monkeypatch.setattr(ft, "get_fulltext_by_chunk_id", lambda kb, cid: "字" * 200)

    tool = ft.make_fulltext_tools()[0]
    content, art = tool.func(title_or_query="x")

    assert art["truncated"] is True
    assert "已截断" in content
    assert "http://example.com/c1" in content  # 截断时给出原件链接


def test_未命中合同(monkeypatch):
    class _Empty:
        def __init__(self, c):
            pass

        def search(self, q, *, top_k=1, **kw):
            return []

    monkeypatch.setattr(ft, "KBRetriever", _Empty)
    tool = ft.make_fulltext_tools()[0]
    content, art = tool.func(title_or_query="不存在的合同")

    assert "未找到" in content
    assert art["matched"] is None


def test_空query提示(monkeypatch):
    monkeypatch.setattr(ft, "KBRetriever", _FakeRetriever)
    tool = ft.make_fulltext_tools()[0]
    content, art = tool.func(title_or_query="  ")
    assert "请提供合同名称" in content


def test_article_body_去掉LLM上下文头():
    text = "资料类型：judicial_article\n标题：X\n来源链接：http://x\n\n城镇房屋租赁合同解释 第一条\n本解释所称……"
    assert ft._article_body(text) == "城镇房屋租赁合同解释 第一条\n本解释所称……"
    assert ft._article_body("没有双换行") == "没有双换行"


def test_司法解释按名取全部条款(monkeypatch):
    """点名某部解释 → 取齐同 doc_id 的全部条款，按顺序、带条数。"""

    class _R:
        def __init__(self, c):
            pass

        def search(self, q, *, top_k=1, **kw):
            return [{"doc_id": "d1", "source_title": "城镇房屋租赁合同解释", "source_url": "http://x"}]

    monkeypatch.setattr(ft, "KBRetriever", _R)
    monkeypatch.setattr(
        ft,
        "_doc_grouped_index",
        lambda kb: {"d1": {"title": "城镇房屋租赁合同解释", "source_url": "http://x",
                            "articles": ["第一条 甲", "第二条 乙", "第三条 丙"]}},
    )

    tool = ft.make_fulltext_tools()[1]  # [0]=合同全文 [1]=司法解释全篇
    content, art = tool.func(name="城镇房屋租赁")

    assert "共 3 条" in content
    assert "第一条 甲" in content and "第三条 丙" in content     # 全部条款都在、按序
    assert art["article_count"] == 3 and art["matched"] == "城镇房屋租赁合同解释"
    assert art["truncated"] is False


def test_司法解释超长按上限截断(monkeypatch):
    class _R:
        def __init__(self, c):
            pass

        def search(self, q, *, top_k=1, **kw):
            return [{"doc_id": "d1", "source_title": "X", "source_url": "http://x"}]

    monkeypatch.setattr(ft, "KBRetriever", _R)
    monkeypatch.setattr(ft.settings, "kb_fulltext_max_chars", 20)
    monkeypatch.setattr(
        ft, "_doc_grouped_index",
        lambda kb: {"d1": {"title": "X", "source_url": "http://x", "articles": ["条" * 50]}},
    )
    tool = ft.make_fulltext_tools()[1]
    content, art = tool.func(name="x")
    assert art["truncated"] is True
    assert "已截断" in content


def test_doc_grouped_index_读磁盘分组(monkeypatch, tmp_path):
    """从 jsonl 按 doc_id 分组、保留顺序、去掉每条的上下文头。"""
    from app.knowledge.registry import KB_BY_KEY

    spec = KB_BY_KEY["judicial"]
    lines = [
        {"doc_id": "d1", "source_title": "解释甲", "source_url": "u1", "text": "头\n\n第一条 A"},
        {"doc_id": "d1", "source_title": "解释甲", "source_url": "u1", "text": "头\n\n第二条 B"},
        {"doc_id": "d2", "source_title": "解释乙", "source_url": "u2", "text": "头\n\n第一条 C"},
    ]
    (tmp_path / spec.chunk_file).write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(ft.settings, "kb_chunks_dir", str(tmp_path))
    ft._doc_grouped_index.cache_clear()
    try:
        idx = ft._doc_grouped_index("judicial")
        assert idx["d1"]["articles"] == ["第一条 A", "第二条 B"]   # 同 doc 顺序保留、头已去
        assert idx["d2"]["title"] == "解释乙"
    finally:
        ft._doc_grouped_index.cache_clear()


def test_fulltext_index_读磁盘(monkeypatch, tmp_path):
    """_fulltext_index 应从该 KB 的源 jsonl 读取每行 text 建 chunk_id→全文 索引。"""
    spec = KB_BY_KEY["standard_contract"]
    (tmp_path / spec.chunk_file).write_text(
        json.dumps({"chunk_id": "k1", "text": "完整合同正文"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ft.settings, "kb_chunks_dir", str(tmp_path))
    ft._fulltext_index.cache_clear()
    try:
        assert ft.get_fulltext_by_chunk_id("standard_contract", "k1") == "完整合同正文"
        assert ft.get_fulltext_by_chunk_id("standard_contract", "missing") is None
    finally:
        ft._fulltext_index.cache_clear()
