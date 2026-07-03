from __future__ import annotations

import logging

from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from app.core.config import settings

logger = logging.getLogger(__name__)


REWRITE_SYSTEM = (
    "你是一个检索查询改写器。给定用户问题，把它改写为更利于稠密向量检索的查询：\n"
    "- 保留原语言（中英文混合时也允许混合）。\n"
    "- 补充可能的同义词、关键术语、英文专有名词；消除指代和歧义。\n"
    "- 不要回答问题，不要添加解释，只输出改写后的查询。\n"
    "- 改写后的查询应当是一个简洁、信息密度高的单段陈述句或关键词组合。"
)

REWRITE_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", REWRITE_SYSTEM),
        ("human", "原始问题：{query}"),
    ]
)


QUERY_REWRITE_TAG = "query_rewrite"


def _build_rewriter_llm() -> BaseChatModel:
    """Build the LLM used exclusively for query rewriting.

    Priority:
    1. REWRITER_PROVIDER + REWRITER_MODEL   (dedicated fast model)
    2. Falls back to the main LLM provider/model via the factory.

    Thinking is always disabled for the rewriter: reasoning adds latency
    without improving retrieval query quality.
    """
    from app.llm.factory import get_chat_llm

    provider = (settings.rewriter_provider or "").strip() or settings.llm_provider
    model = (settings.rewriter_model or "").strip() or None
    # temperature=0 for deterministic rewrites; disable thinking for speed.
    return get_chat_llm(provider=provider, temperature=0.0, model=model, enable_thinking=False)


class QueryRewriter:
    """LLM-based query rewriter (single rewrite, no multi-query / HyDE).

    Short queries (below ``settings.rewriter_min_length`` characters) are
    returned unchanged to avoid an unnecessary LLM round-trip.
    """

    def __init__(self, llm: BaseChatModel | None = None) -> None:
        self._llm = llm or _build_rewriter_llm()
        # Tag the chain so SSE consumers can distinguish rewrite-internal LLM
        # tokens from the main agent's answer tokens.
        self._chain = (REWRITE_PROMPT | self._llm | StrOutputParser()).with_config(
            {"tags": [QUERY_REWRITE_TAG], "run_name": "query_rewrite"}
        )

    def rewrite(self, query: str) -> str:
        query = (query or "").strip()
        if not query:
            return query
        # Skip LLM rewriting for very short queries — they're already concise
        # enough for dense retrieval and the extra round-trip adds latency.
        if len(query) < settings.rewriter_min_length:
            logger.debug("Query too short (%d chars), skipping rewrite.", len(query))
            return query
        try:
            rewritten = self._chain.invoke({"query": query}).strip()
        except Exception:
            logger.exception("Query rewrite failed; falling back to original query")
            return query
        return rewritten or query

    def __call__(self, query: str) -> str:
        return self.rewrite(query)
