"""法律核验工具（问答 Agent 与合同审查 Agent 共用）。

新范式：LLM 先用自身法律知识判断该引用哪条法律，再用这里的工具去法库**核验**——
法库是校验器而非唯一知识源。

两个工具：
- `verify_law_article(law_name, article_no)`：精确核实某条文是否真实存在、原文是什么。
- `search_law(query, law_name)`：语义发现，LLM 想不起准确条文号时找候选。

两个工具的 artifact 形状统一：`citations: [{index, law_name, article_no, citation_text,
chunk_id, content, ...}]`。
- 问答 Agent 用 `index` 做行内 `[n]` 引用（经 SSE 累积、前端映射）。
- 审查 Agent 忽略 `index`，按 `(law_name, article_no)` 把它当「已核实集合」。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from app.core.config import settings
from app.retrieval.article_no import to_cn_article_no
from app.retrieval.query_rewrite import QueryRewriter

logger = logging.getLogger(__name__)

# 本模块产出引用的工具名（用于跨工具调用统计 [n] 偏移）
_CITATION_TOOL_NAMES = {"verify_law_article", "search_law"}

_LAW_REWRITER: QueryRewriter | None = None


def _get_rewriter() -> QueryRewriter:
    global _LAW_REWRITER
    if _LAW_REWRITER is None:
        _LAW_REWRITER = QueryRewriter()
    return _LAW_REWRITER


# ---------------------------------------------------------------------------
# 引用编号 / 格式化（问答侧 [n] 仍需要，跨多次工具调用全局连续）
# ---------------------------------------------------------------------------

def _compute_law_citation_offset(state: Any) -> int:
    """统计本轮已产生的引用数量（跨 verify/search 工具），保证 [n] 编号全局连续。"""
    if state is None:
        return 0
    messages = state.get("messages") if isinstance(state, dict) else getattr(state, "messages", None)
    if not messages:
        return 0
    offset = 0
    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", None) not in _CITATION_TOOL_NAMES:
            continue
        artifact = getattr(msg, "artifact", None)
        if isinstance(artifact, dict):
            offset += len(artifact.get("citations") or [])
    return offset


def _format_law_context(results: list[dict], *, start_index: int = 1) -> str:
    """将法律检索结果格式化为带 [n] 编号的上下文块。"""
    if not results:
        return "(法律知识库中未找到相关法条)"
    parts = []
    for i, r in enumerate(results):
        idx = start_index + i
        citation = r.get("citation_text") or f"{r.get('law_name', '')}{r.get('article_no', '')}"
        chapter = r.get("chapter") or ""
        header = f"{citation} | {chapter}" if chapter else citation
        text = r.get("article_text") or r.get("embedding_text", "")
        parts.append(f"[{idx}] {header}\n{text.strip()}")
    return "\n\n---\n\n".join(parts)


def _result_to_citation(idx: int, r: dict) -> dict[str, Any]:
    """将一条法律检索结果转为统一 citation（含 index 与 (law_name, article_no) 溯源键）。"""
    return {
        "index": idx,
        "source": r.get("citation_text") or f"{r.get('law_name', '')}{r.get('article_no', '')}",
        "page": None,
        "headings": r.get("chapter"),
        "chunk_id": r.get("chunk_id"),
        "doc_id": r.get("doc_id"),
        "content": r.get("article_text") or r.get("embedding_text", ""),
        "law_name": r.get("law_name", ""),
        "article_no": r.get("article_no", ""),
        "citation_text": r.get("citation_text", ""),
        "effective_date": r.get("effective_date", ""),
    }


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

@tool("verify_law_article", response_format="content_and_artifact")
def verify_law_article(
    law_name: str,
    article_no: str,
    state: Annotated[dict, InjectedState],
) -> tuple[str, dict[str, Any]]:
    """核验你想引用的某条法律条文：确认它是否真实存在，并返回真实原文供你比对。

    用法：当你（凭自身法律知识）判断某条款应引用某条法律时，调用本工具核实。
    - law_name:   法律全称，如「中华人民共和国民法典」。不确定可传空字符串。
    - article_no: 条文号，如「第五百三十三条」「第533条」均可（系统会归一化）。

    返回真实条文原文。请**比对你的理解与真实原文**：一致才引用；不一致请修正条文号或弃用。
    若未精确命中，会给出语义最接近的条文，请据其真实条文号引用，不要凭空捏造。
    """
    from app.retrieval.law_retriever import LawRetriever

    law_name = (law_name or "").strip()
    article_no = (article_no or "").strip()
    if not article_no:
        return "(未提供条文号，无法核验)", {"citations": [], "start_index": 1, "precise": False}

    retriever = LawRetriever(k=settings.top_k)
    cn_article = to_cn_article_no(article_no)
    offset = _compute_law_citation_offset(state)
    start_index = offset + 1

    results: list[dict] = []
    precise = False

    if law_name:
        # 先把法名解析到库内真实法名。库里没有这部法律时，明确提示换法名——
        # 绝不把 law_name 丢掉去跨法律堆同号条文（否则会返回一堆张冠李戴的「第 N 条」）。
        canonical = retriever.find_law_in_kb(law_name)
        if canonical is None:
            content = _law_not_in_kb_content(law_name, article_no)
            return content, {
                "start_index": start_index, "citations": [], "precise": False, "law_in_kb": False,
            }
        # 1) 在该法律内精确取条文（中文 / 原始两种写法）
        for q in dict.fromkeys([cn_article, article_no]):
            try:
                results = retriever.fetch_article(article_no=q, law_name=canonical)
            except Exception:
                logger.exception("verify 精确查询失败")
                results = []
            if results:
                precise = True
                break
        # 2) 该法律在库但此条未精确命中 → 仅在该法律内语义兜底（不串到别的法律）
        if not results:
            try:
                results = retriever.search(f"{canonical} {article_no}".strip(), law_name=canonical)
            except Exception:
                logger.exception("verify 语义兜底失败")
                results = []
    else:
        # 没给法名：跨法库纯语义发现候选（应让模型尽量带上 law_name）
        try:
            results = retriever.search(article_no, law_name=None)
        except Exception:
            logger.exception("verify 无法名语义查询失败")
            results = []

    citations = [_result_to_citation(start_index + i, r) for i, r in enumerate(results)]
    content = _format_verify_content(law_name, article_no, results, start_index, precise=precise)
    return content, {"start_index": start_index, "citations": citations, "precise": precise}


def _law_not_in_kb_content(law_name: str, article_no: str) -> str:
    """法库未收录该法律时，提示模型换法名/改用 search_law，而不是张冠李戴。"""
    return (
        f"法库中暂无《{law_name}》这部法律，无法核验「{law_name}{article_no}」，"
        "也不会用其它法律的同号条文来充数。\n"
        "请改用与本问题相关、且确实收录的法律名称重新核验"
        "（可先用 search_law 按关键词检索，确认正确的法律与条文号后再核验）；"
        "若你确信该条文真实存在，可照常引用并标注——系统会标为「未核实」，"
        "但**切勿把该条文号硬安到法库里别的法律上**。"
    )


def _format_verify_content(
    law_name: str, article_no: str, results: list[dict], start_index: int, *, precise: bool
) -> str:
    target = f"{law_name}{article_no}".strip()
    if not results:
        return (
            f"未在法库找到「{target}」（精确与语义均无结果）。"
            "请确认条文号是否正确，或弃用该引用——不要凭空捏造不存在的条文。"
        )
    body = _format_law_context(results, start_index=start_index)
    if precise:
        head = "已在法库核实到以下真实条文。请比对你的理解与原文：一致再引用，不一致请修正条文号或弃用。"
    else:
        head = (
            f"未精确命中「{target}」。法库中语义最接近的真实条文如下，"
            "请按其真实条文号引用，或弃用该引用——不要把你想引的条文号硬安在这些条文上。"
        )
    return f"{head}\n\n{body}"


@tool("search_law", response_format="content_and_artifact")
def search_law(
    query: str,
    state: Annotated[dict, InjectedState],
    law_name: str = "",
) -> tuple[str, dict[str, Any]]:
    """语义检索法库，发现与某主题/场景相关的候选法条。

    用法：当你想引用法律但**想不起准确条文号**时，用本工具找候选，再从结果里
    按真实条文号引用（或对候选用 verify_law_article 进一步确认）。
    - query:    检索意图，如「合同违约赔偿责任」「情势变更」。
    - law_name: **可选**，限定某部法律缩小范围；不限定就**不传或传空字符串**即可。
    """
    from app.retrieval.law_retriever import LawRetriever

    query = (query or "").strip()
    law_name = (law_name or "").strip()
    if not query:
        return "(空查询)", {"rewritten": "", "citations": [], "start_index": 1}

    retriever = LawRetriever(k=settings.top_k)
    rewritten = query
    try:
        rewritten = _get_rewriter().rewrite(query)
    except Exception:
        logger.exception("law query rewrite 失败，使用原始 query")

    try:
        results = retriever.search(rewritten, law_name=law_name or None)
    except Exception:
        logger.exception("law search 失败")
        return "(法律检索失败，无可用上下文)", {
            "rewritten": rewritten, "citations": [], "start_index": 1, "error": "law_search_failed",
        }

    offset = _compute_law_citation_offset(state)
    start_index = offset + 1
    content = _format_law_context(results, start_index=start_index)
    citations = [_result_to_citation(start_index + i, r) for i, r in enumerate(results)]
    return content, {"rewritten": rewritten, "start_index": start_index, "citations": citations}


__all__ = ["verify_law_article", "search_law"]
