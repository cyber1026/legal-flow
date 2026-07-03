"""共享核验工具 verify_law_article / search_law 的单测（mock LawRetriever）。"""

from __future__ import annotations

import app.retrieval.law_retriever as lr_mod
from app.agents.law_tools import search_law, verify_law_article


def _row(law_name: str, article_no: str, chunk_id: str, **extra) -> dict:
    return {
        "law_name": law_name,
        "article_no": article_no,
        "chunk_id": chunk_id,
        "citation_text": extra.get("citation_text", f"《{law_name}》{article_no}"),
        "article_text": extra.get("article_text", "条文原文……"),
        "chapter": extra.get("chapter", ""),
    }


def _patch_retriever(monkeypatch, *, fetch=None, search=None, calls=None, law_in_kb=True):
    """把 app.retrieval.law_retriever.LawRetriever 替换为可控的假实现。

    ``law_in_kb``：模拟「请求的法律是否在库」。True → find_law_in_kb 回显 law_name（在库）；
    False → 返回 None（库内无此法律）。
    """
    class Fake:
        def __init__(self, k=None):
            pass

        def find_law_in_kb(self, law_name):
            return (law_name or None) if law_in_kb else None

        def fetch_article(self, article_no, law_name=None):
            if calls is not None:
                calls.setdefault("fetch", []).append((article_no, law_name))
            return (fetch or {}).get(article_no, [])

        def search(self, query, law_name=None):
            if calls is not None:
                calls.setdefault("search", []).append((query, law_name))
            return search or []

    monkeypatch.setattr(lr_mod, "LawRetriever", Fake)


def test_verify_精确命中且归一化查询(monkeypatch):
    """模型传阿拉伯「第533条」→ 应以中文「第五百三十三条」精确查询并命中。"""
    rows = [_row("中华人民共和国民法典", "第五百三十三条", "c1", article_text="情势变更……")]
    calls: dict = {}
    _patch_retriever(monkeypatch, fetch={"第五百三十三条": rows}, calls=calls)

    content, artifact = verify_law_article.func(
        law_name="中华人民共和国民法典", article_no="第533条", state={"messages": []},
    )

    assert calls["fetch"][0][0] == "第五百三十三条"  # 归一化为中文形式查询
    assert artifact["precise"] is True
    assert len(artifact["citations"]) == 1
    c = artifact["citations"][0]
    assert c["index"] == 1 and c["article_no"] == "第五百三十三条" and c["chunk_id"] == "c1"
    assert "核实" in content


def test_verify_精确未命中降级语义(monkeypatch):
    nearest = [_row("中华人民共和国民法典", "第五百九十条", "c9", article_text="不可抗力……")]
    _patch_retriever(monkeypatch, fetch={}, search=nearest)

    content, artifact = verify_law_article.func(
        law_name="中华人民共和国民法典", article_no="第九千九百九十九条", state={"messages": []},
    )

    assert artifact["precise"] is False
    assert artifact["citations"][0]["article_no"] == "第五百九十条"
    assert "未精确命中" in content


def test_verify_完全找不到(monkeypatch):
    _patch_retriever(monkeypatch, fetch={}, search=[])

    content, artifact = verify_law_article.func(
        law_name="中华人民共和国民法典", article_no="第九千九百九十九条", state={"messages": []},
    )

    assert artifact["precise"] is False
    assert artifact["citations"] == []
    assert "未在法库找到" in content


def test_verify_编号偏移延续(monkeypatch):
    """state 里已有一次工具返回 1 条引用时，本次 index 应从 2 起。"""
    from langchain_core.messages import ToolMessage

    rows = [_row("中华人民共和国民法典", "第五百三十三条", "c1")]
    _patch_retriever(monkeypatch, fetch={"第五百三十三条": rows})

    prev = ToolMessage(
        content="x", tool_call_id="t0", name="search_law",
        artifact={"citations": [{"index": 1}]},
    )
    _, artifact = verify_law_article.func(
        law_name="中华人民共和国民法典", article_no="第五百三十三条",
        state={"messages": [prev]},
    )
    assert artifact["start_index"] == 2 and artifact["citations"][0]["index"] == 2


def test_verify_法库无此法律时提示换法名而不堆同号条文(monkeypatch):
    """法库没有《劳动争议调解仲裁法》→ 提示换法名，且绝不返回别的法律的「第五十条」。"""
    # 即便 fetch/search 配了别的法律的同号条文，也不该被返回（因为法库无此法，提前短路）。
    other = [_row("中华人民共和国劳动法", "第五十条", "x")]
    calls: dict = {}
    _patch_retriever(monkeypatch, fetch={"第五十条": other}, search=other, calls=calls, law_in_kb=False)

    content, artifact = verify_law_article.func(
        law_name="中华人民共和国劳动争议调解仲裁法", article_no="第五十条", state={"messages": []},
    )

    assert artifact["citations"] == []            # 不堆别的法律的同号条文
    assert artifact.get("law_in_kb") is False
    assert "暂无" in content and "search_law" in content
    assert "fetch" not in calls                   # 法库无此法 → 直接短路，没去 fetch/search


def test_search_law_返回候选(monkeypatch):
    rows = [
        _row("中华人民共和国民法典", "第五百七十七条", "a"),
        _row("中华人民共和国民法典", "第五百八十四条", "b"),
    ]
    _patch_retriever(monkeypatch, search=rows)

    content, artifact = search_law.func(
        query="违约赔偿责任", law_name="中华人民共和国民法典", state={"messages": []},
    )

    assert len(artifact["citations"]) == 2
    assert artifact["citations"][0]["index"] == 1
    assert "[1]" in content and "[2]" in content
