#!/usr/bin/env python3
"""审查支撑知识库 Embedding 入库 CLI（第 2–4 层语料 → 5 个 Milvus collection）。

把 ``data/legal_sources/_normalized/chunks`` 下各知识库的 chunk 文件用 BGE-M3 向量化写入对应 collection。

用法：
    python scripts/ingest_legal_kb.py --list                 # 列出 5 个知识库及目标 collection
    python scripts/ingest_legal_kb.py --kb all --rebuild     # 5 库全量重建（drop 后重灌）
    python scripts/ingest_legal_kb.py --kb judicial --rebuild# 只重建司法解释库
    python scripts/ingest_legal_kb.py --kb case              # 追加写入案例库（不 drop）

前置：Milvus 与 Infinity(BGE-M3) 服务可用（见 .env 的 milvus_uri / embedding_base_url）。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from tqdm import tqdm

# 允许以 `python scripts/ingest_legal_kb.py` 直接运行（脚本目录无 __init__.py）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.knowledge.ingest import ingest_kb  # noqa: E402
from app.knowledge.registry import KB_BY_KEY, KB_REGISTRY  # noqa: E402


class _IngestProgress:
    """把入库 progress_callback 转成 tqdm 进度条。"""

    def __init__(self, key: str, *, disabled: bool = False) -> None:
        """初始化某个知识库的入库进度条。"""
        self.key = key
        self.disabled = disabled
        self._bar: tqdm | None = None
        self._done = 0

    def __call__(self, done: int, total: int) -> None:
        """按本批完成数量更新进度条。"""
        if self.disabled:
            return
        if self._bar is None:
            self._bar = tqdm(total=total, desc=f"入库 {self.key}", unit="chunk")
        delta = max(0, done - self._done)
        self._bar.update(delta)
        self._done = done
        if done >= total:
            self.close()

    def close(self) -> None:
        """关闭进度条。"""
        if self._bar is not None:
            self._bar.close()
            self._bar = None


def _ingest_with_progress(spec, *, rebuild: bool, chunks_dir: str | None, disabled: bool) -> int:
    """执行单个知识库入库，并把批处理进度输出到终端。"""
    progress = _IngestProgress(spec.key, disabled=disabled)
    try:
        return ingest_kb(
            spec,
            rebuild=rebuild,
            chunks_dir=chunks_dir,
            progress_callback=progress,
        )
    finally:
        progress.close()


def main() -> None:
    """解析 CLI 参数并执行审查支撑知识库入库。"""
    parser = argparse.ArgumentParser(description="审查支撑知识库 Embedding 入库")
    parser.add_argument("--kb", default="all", help="知识库 key（见 --list）或 all，默认 all")
    parser.add_argument("--rebuild", action="store_true", help="drop 并重建 collection（全量重灌）")
    parser.add_argument("--chunks-dir", default=None, help="覆盖 chunk 目录（默认 settings.kb_chunks_dir）")
    parser.add_argument("--list", action="store_true", help="列出可用知识库后退出")
    parser.add_argument("--no-progress", action="store_true", help="关闭命令行进度条")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    if args.list:
        print(f"{'key':18s} {'collection':28s} chunk_file")
        for spec in KB_REGISTRY:
            print(f"{spec.key:18s} {spec.collection:28s} {spec.chunk_file}")
        return

    t0 = time.time()
    if args.kb == "all":
        results = {}
        for spec in tqdm(KB_REGISTRY, desc="知识库", unit="kb", disable=args.no_progress):
            try:
                results[spec.key] = _ingest_with_progress(
                    spec,
                    rebuild=args.rebuild,
                    chunks_dir=args.chunks_dir,
                    disabled=args.no_progress,
                )
            except Exception:
                logging.exception("[%s] 入库失败", spec.key)
                results[spec.key] = -1
    else:
        spec = KB_BY_KEY.get(args.kb)
        if spec is None:
            parser.error(f"未知知识库 '{args.kb}'，可用：{', '.join(KB_BY_KEY)} 或 all")
        results = {
            spec.key: _ingest_with_progress(
                spec,
                rebuild=args.rebuild,
                chunks_dir=args.chunks_dir,
                disabled=args.no_progress,
            )
        }

    print("\n=== 入库结果 ===")
    for key, n in results.items():
        print(f"  {key:18s} {'失败(见日志)' if n < 0 else f'{n} 条'}")
    print(f"耗时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
