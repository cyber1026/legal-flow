"""审查支撑知识库的 Embedding 入库管线（5 库通用）。

读取 ``settings.kb_chunks_dir`` 下各 KB 的 jsonl，逐条转 Document、批量 ``add_documents`` 进对应
Milvus collection。v1 数据静态：``rebuild=True`` 时整库 ``drop_old`` 重建（最简、幂等）。

程序化用法：
    from app.knowledge.ingest import ingest_all, ingest_kb
    ingest_all(rebuild=True)                       # 5 库全量重建
    ingest_kb(KB_BY_KEY["judicial"], rebuild=True) # 只重建司法解释库

CLI 见 ``scripts/ingest_legal_kb.py``。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from app.core.config import settings
from app.knowledge.registry import KB_REGISTRY, KBSpec
from app.knowledge.vector_store import build_kb_vector_store, chunk_to_document

logger = logging.getLogger(__name__)

# 标准条款库约 1.6 万条，批量大些更快；文本不长，256 安全。
_BATCH_SIZE = 256


def _load_jsonl(path: Path) -> list[dict]:
    """读取 jsonl 的全部非空行为 dict 列表。"""
    chunks: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                chunks.append(json.loads(line))
    return chunks


def ingest_kb(
    spec: KBSpec,
    *,
    rebuild: bool = False,
    chunks_dir: str | Path | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> int:
    """把单个知识库的 chunk 文件向量化写入其 collection，返回写入条数。

    Args:
        spec: 知识库规格。
        rebuild: True → 先 drop 该 collection 再重建（全量重灌）。
        chunks_dir: chunk 目录，默认 ``settings.kb_chunks_dir``。
        progress_callback: 可选 ``callback(done, total)``，每批后回调。
    """
    base = Path(chunks_dir or settings.kb_chunks_dir)
    path = base / spec.chunk_file
    if not path.exists():
        logger.error("[%s] chunk 文件不存在：%s", spec.key, path)
        return 0

    chunks = _load_jsonl(path)
    if not chunks:
        logger.warning("[%s] 空文件，跳过：%s", spec.key, path)
        return 0

    # rebuild 时 drop_old=True 整库重建；否则追加写入同一 collection。
    vs = build_kb_vector_store(spec.collection, drop_old=rebuild)
    docs = [chunk_to_document(c) for c in chunks]

    total = 0
    for i in range(0, len(docs), _BATCH_SIZE):
        batch = docs[i : i + _BATCH_SIZE]
        vs.add_documents(batch)
        total += len(batch)
        if progress_callback:
            progress_callback(total, len(docs))
        logger.debug("[%s] 已写入 %d / %d", spec.key, total, len(docs))

    logger.info("[%s] 入库完成 → collection=%s，共 %d 条", spec.key, spec.collection, total)
    return total


def ingest_all(
    *,
    rebuild: bool = False,
    chunks_dir: str | Path | None = None,
) -> dict[str, int]:
    """遍历 5 个知识库依次入库，返回 {key: 写入条数}（失败的库 count=-1，不中断其余）。"""
    results: dict[str, int] = {}
    for spec in KB_REGISTRY:
        try:
            results[spec.key] = ingest_kb(spec, rebuild=rebuild, chunks_dir=chunks_dir)
        except Exception:
            logger.exception("[%s] 入库失败", spec.key)
            results[spec.key] = -1
    return results


__all__ = ["ingest_kb", "ingest_all"]
