"""合同入库 Pipeline（解析 + 拆条款 + 写 PG + 写 Milvus）。

不包含「风险审查」阶段，那部分由 review_pipeline.py 负责。
将「入库」与「审查」拆开，便于：
- 单独跑入库（只想看条款拆分结果）
- 失败重试更精细（embedding 失败不影响已落地的 clauses）

线程模型：所有内部调用都是同步阻塞（PG + Milvus + Docling/PaddleOCR 都是同步库），
由上层 BackgroundTask 在 worker 线程内执行。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from langsmith import traceable

from app.contracts.clause_splitter import Clause, split_clauses
from app.contracts.milvus_store import add_clauses as add_clause_vectors
from app.contracts.milvus_store import clause_to_document
from app.contracts.milvus_store import delete_by_contract
from app.contracts.parser import parse_contract_file
from app.contracts.parser.base import ParsedDoc
from app.contracts.store import ClauseRecord, ContractRecord, ContractStore
from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IngestResult:
    """入库结果摘要。"""

    contract: ContractRecord
    parsed_doc: ParsedDoc
    clauses: list[Clause]
    clause_records: list[ClauseRecord]
    embedded_count: int


class ContractIngestPipeline:
    """合同上传后的入库流程。

    用法：
        result = ContractIngestPipeline().run(contract_id=42)
    """

    def __init__(self, *, max_clauses: Optional[int] = None) -> None:
        # None 时用配置默认值，便于测试覆写
        self._max_clauses = max_clauses or settings.contract_max_clauses

    @traceable(name="contract_ingest", run_type="chain")
    def run(self, contract_id: int) -> IngestResult:
        """根据 contract_id 跑完整入库流程。

        合同记录在调用前必须已经存在于 contracts 表（由 API 层 create）。

        ``@traceable``：入库（解析/OCR/切分/向量化）是非 LangChain 的同步重活，
        默认不进 LangSmith trace；这里显式标记，让审查 trace 树能看到这段耗时。
        经 ``asyncio.to_thread`` 调用时 contextvars 会复制到线程，故能正确挂到 parse_contract 节点下。
        """
        contract = ContractStore.get_by_id(contract_id)
        if contract is None:
            raise ValueError(f"contract not found: {contract_id}")

        # ── 阶段1：解析 ─────────────────────────────────────────────
        ContractStore.update_status(contract_id, status="parsing")
        parsed = self._parse(Path(contract.storage_path))

        # ── 阶段2：拆条款 ───────────────────────────────────────────
        clauses = self._split(parsed)
        if not clauses:
            ContractStore.update_status(
                contract_id,
                status="failed",
                error="未能从合同中切分出任何条款",
                finish=True,
            )
            raise RuntimeError("clause splitter returned 0 clauses")

        if len(clauses) > self._max_clauses:
            logger.warning(
                "合同条款数 %d 超过上限 %d，截断", len(clauses), self._max_clauses,
            )
            clauses = clauses[: self._max_clauses]

        # 把标题落到 contracts 表，便于列表/详情展示
        ContractStore.update_status(
            contract_id, title=parsed.title or contract.filename
        )

        # ── 阶段3：写 PG ────────────────────────────────────────────
        ContractStore.clear_review_data(contract_id)
        clause_dicts = [
            {
                "clause_id": c.clause_id,
                "section_path": c.section_path,
                "clause_no": c.clause_no,
                "title": c.title,
                "text": c.text,
                "page_no": c.page_no,
                "bbox": c.bbox,
                "chunk_index": c.chunk_index,
            }
            for c in clauses
        ]
        clause_records = ContractStore.insert_clauses(contract_id, clause_dicts)
        ContractStore.update_status(
            contract_id,
            status="embedding",
            parsed_clauses=len(clause_records),
        )

        # ── 阶段4：写 Milvus ────────────────────────────────────────
        contract_title = parsed.title or contract.filename
        embedded = self._embed(
            contract_id=contract_id,
            user_id=contract.user_id,
            contract_title=contract_title,
            clauses=clauses,
            clause_records=clause_records,
        )

        # 入库完毕，更新合同标题并刷新最新合同记录
        updated = ContractStore.update_status(contract_id)
        return IngestResult(
            contract=updated or contract,
            parsed_doc=parsed,
            clauses=clauses,
            clause_records=clause_records,
            embedded_count=embedded,
        )

    # ------------------------------------------------------------------
    # 阶段实现（保持私有，便于将来切流水线/重试）
    # ------------------------------------------------------------------

    @traceable(name="parse_contract_file")
    def _parse(self, file_path: Path) -> ParsedDoc:
        started = time.perf_counter()
        parsed = parse_contract_file(file_path)
        logger.info(
            "文件解析完成 path=%s elapsed=%.0fms", file_path.name, (time.perf_counter() - started) * 1000
        )
        return parsed

    def _split(self, parsed: ParsedDoc) -> list[Clause]:
        return split_clauses(parsed)

    def _embed(
        self,
        *,
        contract_id: int,
        user_id: int,
        contract_title: str,
        clauses: list[Clause],
        clause_records: list[ClauseRecord],
    ) -> int:
        """把 clauses 写入 Milvus contract_chunks。

        失败抛异常由上层捕获；不捕获是因为入库失败时整个 job 应当 fail，
        而不是静默成功但向量缺失。
        """
        if len(clauses) != len(clause_records):
            raise RuntimeError(
                f"clause/record 数量不一致: {len(clauses)} vs {len(clause_records)}"
            )

        documents = []
        for clause, rec in zip(clauses, clause_records):
            chunk_id = f"contract-{contract_id}-{rec.clause_id}"
            documents.append(
                clause_to_document(
                    chunk_id=chunk_id,
                    contract_id=contract_id,
                    user_id=user_id,
                    contract_title=contract_title,
                    section_path=clause.section_path,
                    clause_no=clause.clause_no,
                    clause_title=clause.title,
                    clause_text=clause.text,
                    page_no=clause.page_no,
                    bbox=clause.bbox,
                )
            )
        started = time.perf_counter()
        delete_by_contract(contract_id)
        embedded = add_clause_vectors(documents)
        logger.info(
            "向量化写入完成 contract=%s count=%s elapsed=%.0fms",
            contract_id, embedded, (time.perf_counter() - started) * 1000,
        )
        return embedded


__all__ = ["ContractIngestPipeline", "IngestResult"]
