"""「按名回取整篇」工具（供 supervisor 用），两种：

1. ``get_standard_contract_fulltext`` —— 整份标准示范合同逐字全文。
   背景：整份合同正文最长约 270KB，超 Milvus 单行 dynamic field 上限（65536 字节），入库被截断
   （见 [`vector_store._fit_metadata`](vector_store.py)）。Milvus 只留向量+紧凑元数据+正文摘录，
   完整全文从磁盘 ``standard_contracts_full.jsonl`` 按 chunk_id 回取。

2. ``get_judicial_interpretation`` —— 某部司法解释的**全部条款**（按条号顺序）。
   背景：``search_judicial_interpretations`` 是按主题的 top-k 语义检索，用户**点名某部解释**要看「全文/
   所有条款」时只会拿到零散几条。本工具按名定位到该解释的 doc_id，再从磁盘按文档顺序取齐全部条款。

共同点：凭语义检索 top-1 把「用户给的名称」定位到具体文件，再从磁盘 hydrate 完整内容。逐条款审查用不到
整篇，故只挂 supervisor（会话式问答 / 起草 / redline / 对照），不挂逐条款审查 agent，避免无谓灌爆上下文。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from app.core.config import settings
from app.knowledge.registry import KB_BY_KEY, KBSpec
from app.knowledge.retriever import KBRetriever

logger = logging.getLogger(__name__)

# 整份标准合同库（逐字全文被截断，需回盘取）。
_FULLTEXT_KB = "standard_contract"
# 司法解释库（doc_id 聚合多条条款，可按文档取全篇）。
_JUDICIAL_KB = "judicial"


@lru_cache(maxsize=None)
def _fulltext_index(kb_key: str) -> dict[str, str]:
    """构建 chunk_id → 逐字全文(text) 的磁盘索引（按 KB 缓存）。

    直接读该 KB 的源 jsonl（未截断），取每行的 ``text`` 字段（整份合同正文）。
    """
    spec = KB_BY_KEY[kb_key]
    path = Path(settings.kb_chunks_dir) / spec.chunk_file
    index: dict[str, str] = {}
    if not path.exists():
        logger.error("[fulltext] 源文件不存在：%s", path)
        return index
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            cid = d.get("chunk_id")
            if cid:
                index[cid] = d.get("text") or ""
    return index


def get_fulltext_by_chunk_id(kb_key: str, chunk_id: str) -> str | None:
    """按 chunk_id 从磁盘取该 KB 的逐字全文；不存在返回 None。"""
    return _fulltext_index(kb_key).get(chunk_id)


def _build_fulltext_tool(spec: KBSpec):
    """为指定 KB 构建「定位 + 回取逐字全文」工具。"""

    @tool("get_standard_contract_fulltext", response_format="content_and_artifact")
    def get_standard_contract_fulltext(title_or_query: str) -> tuple[str, dict[str, Any]]:
        """按合同名称或主题，回取**整份官方标准示范合同的逐字全文**（用于起草 / redline / 整份对照）。

        与 search_standard_contracts 的区别：后者返回的是「结构 + 摘录 + 总结」的审查参照；本工具返回
        命中示范合同的**完整原文**（从磁盘取，不截断到摘要）。title_or_query 写合同名称或类型
        （如「房屋租赁合同」「建设工程施工合同」）。注意：这是**示范文本参照**，不是法条。
        """
        q = (title_or_query or "").strip()
        if not q:
            return "（请提供合同名称或类型）", {"kb": spec.key, "matched": None}

        hits = KBRetriever(spec.collection).search(q, top_k=1)
        if not hits:
            return f"（未找到与「{q}」匹配的标准示范合同。）", {"kb": spec.key, "matched": None}

        hit = hits[0]
        chunk_id = hit.get("chunk_id", "")
        title = hit.get("contract_title") or hit.get("display_text") or chunk_id
        source_url = hit.get("source_url", "")

        full = get_fulltext_by_chunk_id(spec.key, chunk_id) or ""
        if not full:
            return (
                f"（已匹配《{title}》，但磁盘未取到其逐字全文。）",
                {"kb": spec.key, "matched": title, "chunk_id": chunk_id, "source_url": source_url},
            )

        limit = settings.kb_fulltext_max_chars
        truncated = len(full) > limit
        body = full[:limit] + (f"\n\n…（全文较长，已截断；完整原件见：{source_url}）" if truncated else "")
        header = f"《{title}》官方标准示范合同全文（参照，非法条；来源：{source_url}）：\n\n"
        return header + body, {
            "kb": spec.key,
            "matched": title,
            "chunk_id": chunk_id,
            "source_url": source_url,
            "truncated": truncated,
            "full_chars": len(full),
        }

    return get_standard_contract_fulltext


# ---------------------------------------------------------------------------
# 司法解释：按文档(doc_id)聚合全部条款
# ---------------------------------------------------------------------------

def _article_body(text: str) -> str:
    """从 chunk 的 text 去掉 LLM 上下文头，取「条号 + 条文正文」那段。

    judicial chunk 的 text 结构固定为：``<LLM上下文头>\\n\\n<引用>\\n<条文正文>``，
    按首个双换行切分即得干净正文；无双换行则原样返回。
    """
    parts = (text or "").split("\n\n", 1)
    return (parts[1] if len(parts) > 1 else (text or "")).strip()


@lru_cache(maxsize=None)
def _doc_grouped_index(kb_key: str) -> dict[str, dict[str, Any]]:
    """构建 doc_id → {title, source_url, articles:[条文正文,...]} 索引（保留 jsonl 即文档顺序）。"""
    spec = KB_BY_KEY[kb_key]
    path = Path(settings.kb_chunks_dir) / spec.chunk_file
    groups: dict[str, dict[str, Any]] = {}
    if not path.exists():
        logger.error("[fulltext] 源文件不存在：%s", path)
        return groups
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            doc_id = d.get("doc_id")
            if not doc_id:
                continue
            g = groups.setdefault(
                doc_id,
                {
                    "title": d.get("source_title") or d.get("title") or "",
                    "source_url": d.get("source_url", ""),
                    "articles": [],
                },
            )
            g["articles"].append(_article_body(d.get("text") or ""))
    return groups


def _build_judicial_whole_tool(spec: KBSpec):
    """构建「按名回取某部司法解释全部条款」工具。"""

    @tool("get_judicial_interpretation", response_format="content_and_artifact")
    def get_judicial_interpretation(name: str) -> tuple[str, dict[str, Any]]:
        """按名称回取**某部司法解释的全部条款**（按条号顺序的完整原文）。

        与 search_judicial_interpretations 的区别：后者是按主题返回零散几条的语义检索；当用户**点名某部
        司法解释**、要看它「全文 / 所有条款 / 一共多少条」时，用本工具取齐整部解释。name 写解释名称（全名
        或简称均可，如「城镇房屋租赁合同解释」「民法典合同编通则解释」）。返回完整条款，是**参照，非法条核验**。
        """
        q = (name or "").strip()
        if not q:
            return "（请提供司法解释名称）", {"kb": spec.key, "matched": None}

        hits = KBRetriever(spec.collection).search(q, top_k=1)
        if not hits:
            return f"（未找到与「{q}」匹配的司法解释。）", {"kb": spec.key, "matched": None}

        doc_id = hits[0].get("doc_id", "")
        grp = _doc_grouped_index(spec.key).get(doc_id)
        if not grp or not grp.get("articles"):
            title = hits[0].get("source_title") or hits[0].get("title") or q
            return (
                f"（已匹配《{title}》，但磁盘未取到其条款。）",
                {"kb": spec.key, "matched": title, "doc_id": doc_id},
            )

        title = grp["title"] or hits[0].get("source_title") or q
        source_url = grp.get("source_url") or hits[0].get("source_url", "")
        articles = grp["articles"]
        body = "\n\n".join(articles)

        limit = settings.kb_fulltext_max_chars
        truncated = len(body) > limit
        if truncated:
            body = body[:limit] + f"\n\n…（条款较多，已截断；完整原件见：{source_url}）"
        header = f"《{title}》全部条款（共 {len(articles)} 条；参照，非法条；来源：{source_url}）：\n\n"
        return header + body, {
            "kb": spec.key,
            "matched": title,
            "doc_id": doc_id,
            "article_count": len(articles),
            "truncated": truncated,
            "source_url": source_url,
        }

    return get_judicial_interpretation


def make_fulltext_tools() -> list:
    """构建「按名回取整篇」工具（标准合同逐字全文 + 司法解释全部条款；供 supervisor 挂载）。"""
    return [
        _build_fulltext_tool(KB_BY_KEY[_FULLTEXT_KB]),
        _build_judicial_whole_tool(KB_BY_KEY[_JUDICIAL_KB]),
    ]


__all__ = ["get_fulltext_by_chunk_id", "make_fulltext_tools"]
