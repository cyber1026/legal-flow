from __future__ import annotations

from functools import lru_cache

from langchain_openai import OpenAIEmbeddings

from app.core.config import settings


@lru_cache(maxsize=1)
def get_embeddings() -> OpenAIEmbeddings:
    """Return a singleton OpenAI-compatible embedding client pointed at Infinity.

    Infinity exposes an OpenAI-compatible `/v1/embeddings` endpoint, so we reuse
    LangChain's `OpenAIEmbeddings`. The API key is irrelevant for a local
    Infinity deployment, but the client requires a non-empty value.
    """
    return OpenAIEmbeddings(
        model=settings.embedding_model,
        base_url=settings.embedding_base_url,
        api_key="not-needed",
        timeout=settings.kb_retrieve_timeout,
        check_embedding_ctx_length=False,
        tiktoken_enabled=False,
    )
