"""合同审查相关的 PostgreSQL 持久化层。

对外暴露 ContractStore 静态方法，覆盖合同、条款、审查意见、条款级风险评估、
一致性事实与一致性审查结果的 CRUD 操作。

所有读接口都强制带 `user_id` 过滤（ownership 校验在 store 层兜底，避免 API 层
漏写也不会越权）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.datetime_utils import to_utc_datetime
from app.db import get_conn


# ---------------------------------------------------------------------------
# 数据类型
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ContractRecord:
    """`contracts` 表的一行。"""

    id: int
    user_id: int
    session_id: Optional[str]
    job_id: str
    filename: str
    mime: str
    doc_type: str
    storage_path: str
    title: str
    status: str
    parsed_clauses: int
    risk_count: int
    error: Optional[str]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    created_at: datetime
    # 委托人立场（甲方/乙方/中立/未知）；为立场 HITL 预留，默认未知。
    party_stance: str = "未知"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "job_id": self.job_id,
            "filename": self.filename,
            "mime": self.mime,
            "doc_type": self.doc_type,
            "title": self.title,
            "status": self.status,
            "parsed_clauses": self.parsed_clauses,
            "risk_count": self.risk_count,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "created_at": self.created_at.isoformat(),
            "party_stance": self.party_stance,
        }


@dataclass(slots=True)
class ClauseRecord:
    """`contract_clauses` 表的一行。"""

    id: int
    contract_id: int
    clause_id: str
    section_path: str
    clause_no: str
    title: str
    text: str
    page_no: Optional[int]
    bbox: Optional[list[float]]
    chunk_index: int
    review_status: str
    review_has_risk: bool
    reasoning: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "contract_id": self.contract_id,
            "clause_id": self.clause_id,
            "section_path": self.section_path,
            "clause_no": self.clause_no,
            "title": self.title,
            "text": self.text,
            "page_no": self.page_no,
            "bbox": self.bbox,
            "chunk_index": self.chunk_index,
            "review_status": self.review_status,
            "review_has_risk": self.review_has_risk,
            "reasoning": self.reasoning,
        }


@dataclass(slots=True)
class RiskCitationRecord:
    id: int
    risk_id: int
    law_name: str
    article_no: str
    citation_text: str
    chunk_id: str
    excerpt: str
    verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "risk_id": self.risk_id,
            "law_name": self.law_name,
            "article_no": self.article_no,
            "citation_text": self.citation_text,
            "chunk_id": self.chunk_id,
            "excerpt": self.excerpt,
            "verified": self.verified,
        }


@dataclass(slots=True)
class RiskItemRecord:
    id: int
    contract_id: int
    clause_id_ref: int
    opinion_type: str
    review_dimension: str
    risk_level: str
    description: str
    suggestion: str
    confidence: float
    created_at: datetime
    citations: list[RiskCitationRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "contract_id": self.contract_id,
            "clause_id_ref": self.clause_id_ref,
            "opinion_type": self.opinion_type,
            "review_dimension": self.review_dimension,
            "risk_level": self.risk_level,
            "description": self.description,
            "suggestion": self.suggestion,
            "confidence": self.confidence,
            "created_at": self.created_at.isoformat(),
            "citations": [c.to_dict() for c in self.citations],
        }


@dataclass(slots=True)
class ReviewOpinionCitationRecord:
    id: int
    opinion_id: int
    law_name: str
    article_no: str
    citation_text: str
    chunk_id: str
    excerpt: str
    verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "opinion_id": self.opinion_id,
            "law_name": self.law_name,
            "article_no": self.article_no,
            "citation_text": self.citation_text,
            "chunk_id": self.chunk_id,
            "excerpt": self.excerpt,
            "verified": self.verified,
        }


@dataclass(slots=True)
class ReviewOpinionRecord:
    id: int
    contract_id: int
    clause_id_ref: int
    opinion_type: str
    review_dimension: str
    finding: str
    recommendation: str
    confidence: float
    created_at: datetime
    citations: list[ReviewOpinionCitationRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "contract_id": self.contract_id,
            "clause_id_ref": self.clause_id_ref,
            "opinion_type": self.opinion_type,
            "review_dimension": self.review_dimension,
            "finding": self.finding,
            "recommendation": self.recommendation,
            "confidence": self.confidence,
            "created_at": self.created_at.isoformat(),
            "citations": [c.to_dict() for c in self.citations],
        }


@dataclass(slots=True)
class ClauseRiskAssessmentRecord:
    id: int
    contract_id: int
    clause_id_ref: int
    risk_level: str
    rationale: str
    affected_party: str
    confidence: float
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "contract_id": self.contract_id,
            "clause_id_ref": self.clause_id_ref,
            "risk_level": self.risk_level,
            "rationale": self.rationale,
            "affected_party": self.affected_party,
            "confidence": self.confidence,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(slots=True)
class ConsistencyFactRecord:
    id: int
    contract_id: int
    clause_id_ref: int
    category: str
    fact_key: str
    party: str
    value_text: str
    normalized_value: str
    span_text: str
    related_text: str
    confidence: float
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "contract_id": self.contract_id,
            "clause_id_ref": self.clause_id_ref,
            "category": self.category,
            "key": self.fact_key,
            "party": self.party,
            "value_text": self.value_text,
            "normalized_value": self.normalized_value,
            "span_text": self.span_text,
            "related_text": self.related_text,
            "confidence": self.confidence,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(slots=True)
class ConsistencyOpinionRecord:
    id: int
    contract_id: int
    opinion_type: str
    review_dimension: str
    finding: str
    recommendation: str
    related_clause_ids: list[str]
    evidence_facts: list[str]
    confidence: float
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "contract_id": self.contract_id,
            "opinion_type": self.opinion_type,
            "review_dimension": self.review_dimension,
            "finding": self.finding,
            "recommendation": self.recommendation,
            "related_clause_ids": self.related_clause_ids,
            "evidence_facts": self.evidence_facts,
            "confidence": self.confidence,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(slots=True)
class ConsistencyRiskAssessmentRecord:
    id: int
    contract_id: int
    risk_level: str
    rationale: str
    affected_party: str
    confidence: float
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "contract_id": self.contract_id,
            "risk_level": self.risk_level,
            "rationale": self.rationale,
            "affected_party": self.affected_party,
            "confidence": self.confidence,
            "created_at": self.created_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# 行转换
# ---------------------------------------------------------------------------

def _row_to_contract(row) -> ContractRecord:
    return ContractRecord(
        id=row["id"],
        user_id=row["user_id"],
        session_id=row.get("session_id"),
        job_id=row["job_id"],
        filename=row["filename"],
        mime=row["mime"],
        doc_type=row["doc_type"],
        storage_path=row["storage_path"],
        title=row["title"],
        status=row["status"],
        parsed_clauses=row["parsed_clauses"],
        risk_count=row["risk_count"],
        error=row["error"],
        started_at=to_utc_datetime(row["started_at"]) if row["started_at"] else None,
        finished_at=to_utc_datetime(row["finished_at"]) if row["finished_at"] else None,
        created_at=to_utc_datetime(row["created_at"]),
        party_stance=row.get("party_stance") or "未知",
    )


def _row_to_clause(row) -> ClauseRecord:
    bbox_raw = row.get("bbox_json")
    bbox: Optional[list[float]] = None
    if bbox_raw:
        try:
            parsed = json.loads(bbox_raw)
            if isinstance(parsed, list):
                bbox = [float(x) for x in parsed[:4]] if len(parsed) >= 4 else None
        except (TypeError, ValueError, json.JSONDecodeError):
            bbox = None
    reasoning_raw = row.get("reasoning_json")
    reasoning: list[dict[str, Any]] = []
    if reasoning_raw:
        try:
            parsed_reasoning = json.loads(reasoning_raw)
            if isinstance(parsed_reasoning, list):
                reasoning = [item for item in parsed_reasoning if isinstance(item, dict)]
        except (TypeError, ValueError, json.JSONDecodeError):
            reasoning = []
    return ClauseRecord(
        id=row["id"],
        contract_id=row["contract_id"],
        clause_id=row["clause_id"],
        section_path=row["section_path"],
        clause_no=row["clause_no"],
        title=row["title"],
        text=row["text"],
        page_no=row["page_no"],
        bbox=bbox,
        chunk_index=row["chunk_index"],
        review_status=row.get("review_status") or "pending",
        review_has_risk=bool(row.get("review_has_risk", False)),
        reasoning=reasoning,
    )


def _row_to_risk(row) -> RiskItemRecord:
    return RiskItemRecord(
        id=row["id"],
        contract_id=row["contract_id"],
        clause_id_ref=row["clause_id_ref"],
        opinion_type=row.get("opinion_type") or "提醒",
        review_dimension=row.get("review_dimension") or "内容合法性",
        risk_level=str(row["risk_level"]).lower(),
        description=row["description"],
        suggestion=row["suggestion"],
        confidence=float(row["confidence"] or 0.0),
        created_at=to_utc_datetime(row["created_at"]),
    )


def _row_to_citation(row) -> RiskCitationRecord:
    return RiskCitationRecord(
        id=row["id"],
        risk_id=row["risk_id"],
        law_name=row["law_name"],
        article_no=row["article_no"],
        citation_text=row["citation_text"],
        chunk_id=row["chunk_id"],
        excerpt=row["excerpt"],
        verified=bool(row.get("verified", False)),
    )


def _row_to_review_opinion(row) -> ReviewOpinionRecord:
    return ReviewOpinionRecord(
        id=row["id"],
        contract_id=row["contract_id"],
        clause_id_ref=row["clause_id_ref"],
        opinion_type=row["opinion_type"],
        review_dimension=row["review_dimension"],
        finding=row["finding"],
        recommendation=row["recommendation"],
        confidence=float(row["confidence"] or 0.0),
        created_at=to_utc_datetime(row["created_at"]),
    )


def _row_to_review_opinion_citation(row) -> ReviewOpinionCitationRecord:
    return ReviewOpinionCitationRecord(
        id=row["id"],
        opinion_id=row["opinion_id"],
        law_name=row["law_name"],
        article_no=row["article_no"],
        citation_text=row["citation_text"],
        chunk_id=row["chunk_id"],
        excerpt=row["excerpt"],
        verified=bool(row.get("verified", False)),
    )


def _row_to_clause_risk_assessment(row) -> ClauseRiskAssessmentRecord:
    return ClauseRiskAssessmentRecord(
        id=row["id"],
        contract_id=row["contract_id"],
        clause_id_ref=row["clause_id_ref"],
        risk_level=str(row["risk_level"]).lower(),
        rationale=row["rationale"],
        affected_party=row["affected_party"],
        confidence=float(row["confidence"] or 0.0),
        created_at=to_utc_datetime(row["created_at"]),
    )


def _row_to_consistency_fact(row) -> ConsistencyFactRecord:
    return ConsistencyFactRecord(
        id=row["id"],
        contract_id=row["contract_id"],
        clause_id_ref=row["clause_id_ref"],
        category=row["category"],
        fact_key=row["fact_key"],
        party=row["party"],
        value_text=row["value_text"],
        normalized_value=row["normalized_value"],
        span_text=row["span_text"],
        related_text=row["related_text"],
        confidence=float(row["confidence"] or 0.0),
        created_at=to_utc_datetime(row["created_at"]),
    )


def _loads_json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _row_to_consistency_opinion(row) -> ConsistencyOpinionRecord:
    evidence = _loads_json_list(row.get("evidence_facts_json"))
    return ConsistencyOpinionRecord(
        id=row["id"],
        contract_id=row["contract_id"],
        opinion_type=row["opinion_type"],
        review_dimension=row["review_dimension"],
        finding=row["finding"],
        recommendation=row["recommendation"],
        related_clause_ids=[str(x) for x in _loads_json_list(row.get("related_clause_ids_json"))],
        # 现在统一存字符串摘要；历史数据里残留的 dict 兜底序列化为字符串，避免读取丢失。
        evidence_facts=[
            x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in evidence
        ],
        confidence=float(row["confidence"] or 0.0),
        created_at=to_utc_datetime(row["created_at"]),
    )


def _row_to_consistency_risk_assessment(row) -> ConsistencyRiskAssessmentRecord:
    return ConsistencyRiskAssessmentRecord(
        id=row["id"],
        contract_id=row["contract_id"],
        risk_level=str(row["risk_level"]).lower(),
        rationale=row["rationale"],
        affected_party=row["affected_party"],
        confidence=float(row["confidence"] or 0.0),
        created_at=to_utc_datetime(row["created_at"]),
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

_CONTRACT_COLS = (
    "id, user_id, session_id, job_id, filename, mime, doc_type, storage_path, title, "
    "status, parsed_clauses, risk_count, error, started_at, finished_at, "
    "created_at, party_stance"
)


class ContractStore:
    """Contract / Clause / Risk / Citation 的 CRUD 静态类。"""

    # ── contracts ─────────────────────────────────────────────────────

    @staticmethod
    def create(
        *,
        user_id: int,
        job_id: str,
        filename: str,
        mime: str,
        doc_type: str,
        storage_path: str,
        session_id: Optional[str] = None,
    ) -> ContractRecord:
        with get_conn() as conn, conn.cursor() as cur:
            # started_at 留空：表示「审查开始时间」，由 update_status(start=True) 在真正起审时填，
            # 不再等于上传时间——否则前端会把审查气泡按上传时刻排到会话最顶部。
            cur.execute(
                f"INSERT INTO contracts(user_id, session_id, job_id, filename, mime, "
                f"doc_type, storage_path, status) "
                f"VALUES(%s, %s, %s, %s, %s, %s, %s, 'pending') "
                f"RETURNING {_CONTRACT_COLS}",
                (user_id, session_id, job_id, filename, mime, doc_type, storage_path),
            )
            row = cur.fetchone()
        return _row_to_contract(row)

    @staticmethod
    def update_status(
        contract_id: int,
        *,
        status: Optional[str] = None,
        title: Optional[str] = None,
        parsed_clauses: Optional[int] = None,
        risk_count: Optional[int] = None,
        error: Optional[str] = None,
        start: bool = False,
        finish: bool = False,
    ) -> Optional[ContractRecord]:
        """部分更新 contracts 字段（None 表示不变）。

        start=True 把 started_at 置为 NOW()（审查起始时刻，供前端时间线排序）；
        finish=True 把 finished_at 置为 NOW()。
        """
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = %s")
            params.append(status)
        if title is not None:
            sets.append("title = %s")
            params.append(title)
        if parsed_clauses is not None:
            sets.append("parsed_clauses = %s")
            params.append(parsed_clauses)
        if risk_count is not None:
            sets.append("risk_count = %s")
            params.append(risk_count)
        if error is not None:
            sets.append("error = %s")
            params.append(error)
        if start:
            sets.append("started_at = NOW()")
        if finish:
            sets.append("finished_at = NOW()")
        if not sets:
            return ContractStore.get_by_id(contract_id)

        params.append(contract_id)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE contracts SET {', '.join(sets)} WHERE id = %s "
                f"RETURNING {_CONTRACT_COLS}",
                tuple(params),
            )
            row = cur.fetchone()
        return _row_to_contract(row) if row else None

    @staticmethod
    def update_party_stance(contract_id: int, stance: str) -> None:
        """更新合同的委托人立场（甲方/乙方/中立/未知）。"""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE contracts SET party_stance = %s WHERE id = %s",
                (stance, contract_id),
            )

    @staticmethod
    def get_by_id(contract_id: int) -> Optional[ContractRecord]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_CONTRACT_COLS} FROM contracts WHERE id = %s",
                (contract_id,),
            )
            row = cur.fetchone()
        return _row_to_contract(row) if row else None

    @staticmethod
    def get_by_job(job_id: str, user_id: int) -> Optional[ContractRecord]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_CONTRACT_COLS} FROM contracts "
                f"WHERE job_id = %s AND user_id = %s",
                (job_id, user_id),
            )
            row = cur.fetchone()
        return _row_to_contract(row) if row else None

    @staticmethod
    def get_owned(contract_id: int, user_id: int) -> Optional[ContractRecord]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_CONTRACT_COLS} FROM contracts "
                f"WHERE id = %s AND user_id = %s",
                (contract_id, user_id),
            )
            row = cur.fetchone()
        return _row_to_contract(row) if row else None

    @staticmethod
    def get_by_session(session_id: str, user_id: int) -> Optional[ContractRecord]:
        """取会话关联的合同（一会话一合同；多条时取最新一条）。"""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_CONTRACT_COLS} FROM contracts "
                f"WHERE session_id = %s AND user_id = %s "
                f"ORDER BY created_at DESC LIMIT 1",
                (session_id, user_id),
            )
            row = cur.fetchone()
        return _row_to_contract(row) if row else None

    @staticmethod
    def list_by_session(session_id: str, user_id: int) -> list[ContractRecord]:
        """列出会话关联的全部合同，按创建时间倒序。"""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_CONTRACT_COLS} FROM contracts "
                f"WHERE session_id = %s AND user_id = %s "
                f"ORDER BY created_at DESC",
                (session_id, user_id),
            )
            rows = cur.fetchall()
        return [_row_to_contract(r) for r in rows]

    @staticmethod
    def list_for_user(user_id: int, limit: int = 100) -> list[ContractRecord]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_CONTRACT_COLS} FROM contracts WHERE user_id = %s "
                f"ORDER BY created_at DESC LIMIT %s",
                (user_id, limit),
            )
            rows = cur.fetchall()
        return [_row_to_contract(r) for r in rows]

    @staticmethod
    def delete(contract_id: int, user_id: int) -> bool:
        """级联删除合同及其条款/风险（FK ON DELETE CASCADE）。"""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM contracts WHERE id = %s AND user_id = %s",
                (contract_id, user_id),
            )
            return cur.rowcount > 0

    @staticmethod
    def clear_review_data(contract_id: int) -> None:
        """清理某合同已生成的条款、意见、风险评估和引用，用于同一合同重跑。"""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM risk_items WHERE contract_id = %s", (contract_id,))
            cur.execute("DELETE FROM review_opinions WHERE contract_id = %s", (contract_id,))
            cur.execute("DELETE FROM clause_risk_assessments WHERE contract_id = %s", (contract_id,))
            cur.execute("DELETE FROM contract_consistency_facts WHERE contract_id = %s", (contract_id,))
            cur.execute("DELETE FROM contract_consistency_opinions WHERE contract_id = %s", (contract_id,))
            cur.execute(
                "DELETE FROM contract_consistency_risk_assessments WHERE contract_id = %s",
                (contract_id,),
            )
            cur.execute(
                "DELETE FROM contract_clauses WHERE contract_id = %s",
                (contract_id,),
            )

    # ── clauses ───────────────────────────────────────────────────────

    @staticmethod
    def insert_clauses(contract_id: int, clauses: list[dict[str, Any]]) -> list[ClauseRecord]:
        """批量写入条款。clauses 中每个 dict 字段对照 ClauseRecord。"""
        if not clauses:
            return []
        records: list[ClauseRecord] = []
        with get_conn() as conn, conn.cursor() as cur:
            for c in clauses:
                bbox_json = json.dumps(c["bbox"], ensure_ascii=False) if c.get("bbox") else None
                cur.execute(
                    "INSERT INTO contract_clauses(contract_id, clause_id, section_path, "
                    "clause_no, title, text, page_no, bbox_json, chunk_index) "
                    "VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "RETURNING id, contract_id, clause_id, section_path, clause_no, "
                    "title, text, page_no, bbox_json, chunk_index, review_status, "
                    "review_has_risk, reasoning_json",
                    (
                        contract_id,
                        c["clause_id"],
                        c.get("section_path", ""),
                        c.get("clause_no", ""),
                        c.get("title", ""),
                        c["text"],
                        c.get("page_no"),
                        bbox_json,
                        c.get("chunk_index", 0),
                    ),
                )
                records.append(_row_to_clause(cur.fetchone()))
        return records

    @staticmethod
    def list_clauses(contract_id: int) -> list[ClauseRecord]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, contract_id, clause_id, section_path, clause_no, title, "
                "text, page_no, bbox_json, chunk_index, review_status, "
                "review_has_risk, reasoning_json "
                "FROM contract_clauses WHERE contract_id = %s ORDER BY chunk_index ASC",
                (contract_id,),
            )
            rows = cur.fetchall()
        return [_row_to_clause(r) for r in rows]

    @staticmethod
    def get_clause(contract_id: int, clause_id: str) -> Optional[ClauseRecord]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, contract_id, clause_id, section_path, clause_no, title, "
                "text, page_no, bbox_json, chunk_index, review_status, "
                "review_has_risk, reasoning_json "
                "FROM contract_clauses WHERE contract_id = %s AND clause_id = %s",
                (contract_id, clause_id),
            )
            row = cur.fetchone()
        return _row_to_clause(row) if row else None

    @staticmethod
    def update_clause_review(
        clause_db_id: int,
        *,
        review_status: str,
        review_has_risk: bool | None = None,
        reasoning: list[dict[str, Any]] | None = None,
    ) -> Optional[ClauseRecord]:
        sets = ["review_status = %s"]
        params: list[Any] = [review_status]
        if review_has_risk is not None:
            sets.append("review_has_risk = %s")
            params.append(review_has_risk)
        if reasoning is not None:
            sets.append("reasoning_json = %s")
            params.append(json.dumps(reasoning, ensure_ascii=False))
        params.append(clause_db_id)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE contract_clauses SET "
                + ", ".join(sets)
                + " WHERE id = %s "
                  "RETURNING id, contract_id, clause_id, section_path, clause_no, title, "
                  "text, page_no, bbox_json, chunk_index, review_status, review_has_risk, "
                  "reasoning_json",
                tuple(params),
            )
            row = cur.fetchone()
        return _row_to_clause(row) if row else None

    # ── risks ─────────────────────────────────────────────────────────

    @staticmethod
    def insert_risk(
        *,
        contract_id: int,
        clause_db_id: int,
        opinion_type: str,
        review_dimension: str,
        risk_level: str,
        description: str,
        suggestion: str,
        confidence: float,
        citations: list[dict[str, Any]],
    ) -> RiskItemRecord:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO risk_items(contract_id, clause_id_ref, opinion_type, "
                "review_dimension, risk_level, description, suggestion, confidence) "
                "VALUES(%s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id, contract_id, clause_id_ref, opinion_type, review_dimension, "
                "risk_level, description, suggestion, confidence, created_at",
                (
                    contract_id,
                    clause_db_id,
                    opinion_type,
                    review_dimension,
                    risk_level.lower(),
                    description,
                    suggestion,
                    float(confidence),
                ),
            )
            risk_row = cur.fetchone()
            risk = _row_to_risk(risk_row)
            for cit in citations or []:
                cur.execute(
                    "INSERT INTO risk_citations(risk_id, law_name, article_no, "
                    "citation_text, chunk_id, excerpt, verified) "
                    "VALUES(%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (risk_id, law_name, article_no) DO NOTHING "
                    "RETURNING id, risk_id, law_name, article_no, citation_text, "
                    "chunk_id, excerpt, verified",
                    (
                        risk.id,
                        cit.get("law_name", ""),
                        cit.get("article_no", ""),
                        cit.get("citation_text", ""),
                        cit.get("chunk_id", ""),
                        cit.get("excerpt", ""),
                        bool(cit.get("verified", False)),
                    ),
                )
                cit_row = cur.fetchone()
                if cit_row:
                    risk.citations.append(_row_to_citation(cit_row))
        return risk

    @staticmethod
    def list_risks(contract_id: int) -> list[RiskItemRecord]:
        """列出合同所有 risk_items，附带 citations。一次性 join 拉回避免 N+1。"""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, contract_id, clause_id_ref, opinion_type, review_dimension, "
                "risk_level, description, suggestion, confidence, created_at "
                "FROM risk_items WHERE contract_id = %s ORDER BY id ASC",
                (contract_id,),
            )
            risk_rows = cur.fetchall()
            risks: dict[int, RiskItemRecord] = {}
            for r in risk_rows:
                rec = _row_to_risk(r)
                risks[rec.id] = rec
            if risks:
                cur.execute(
                    "SELECT id, risk_id, law_name, article_no, citation_text, chunk_id, "
                    "excerpt, verified FROM risk_citations WHERE risk_id = ANY(%s) ORDER BY id ASC",
                    (list(risks.keys()),),
                )
                for c in cur.fetchall():
                    cit = _row_to_citation(c)
                    if cit.risk_id in risks:
                        risks[cit.risk_id].citations.append(cit)
        return list(risks.values())

    # ── review opinions ───────────────────────────────────────────────

    @staticmethod
    def insert_review_opinion(
        *,
        contract_id: int,
        clause_db_id: int,
        opinion_type: str,
        review_dimension: str,
        finding: str,
        recommendation: str,
        confidence: float,
        citations: list[dict[str, Any]],
    ) -> ReviewOpinionRecord:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO review_opinions(contract_id, clause_id_ref, opinion_type, "
                "review_dimension, finding, recommendation, confidence) "
                "VALUES(%s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id, contract_id, clause_id_ref, opinion_type, review_dimension, "
                "finding, recommendation, confidence, created_at",
                (
                    contract_id,
                    clause_db_id,
                    opinion_type,
                    review_dimension,
                    finding,
                    recommendation,
                    float(confidence),
                ),
            )
            opinion = _row_to_review_opinion(cur.fetchone())
            for cit in citations or []:
                cur.execute(
                    "INSERT INTO review_opinion_citations(opinion_id, law_name, article_no, "
                    "citation_text, chunk_id, excerpt, verified) "
                    "VALUES(%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (opinion_id, law_name, article_no) DO NOTHING "
                    "RETURNING id, opinion_id, law_name, article_no, citation_text, "
                    "chunk_id, excerpt, verified",
                    (
                        opinion.id,
                        cit.get("law_name", ""),
                        cit.get("article_no", ""),
                        cit.get("citation_text", ""),
                        cit.get("chunk_id", ""),
                        cit.get("excerpt", ""),
                        bool(cit.get("verified", False)),
                    ),
                )
                cit_row = cur.fetchone()
                if cit_row:
                    opinion.citations.append(_row_to_review_opinion_citation(cit_row))
        return opinion

    @staticmethod
    def list_review_opinions(contract_id: int) -> list[ReviewOpinionRecord]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, contract_id, clause_id_ref, opinion_type, review_dimension, "
                "finding, recommendation, confidence, created_at "
                "FROM review_opinions WHERE contract_id = %s ORDER BY id ASC",
                (contract_id,),
            )
            rows = cur.fetchall()
            opinions: dict[int, ReviewOpinionRecord] = {}
            for row in rows:
                rec = _row_to_review_opinion(row)
                opinions[rec.id] = rec
            if opinions:
                cur.execute(
                    "SELECT id, opinion_id, law_name, article_no, citation_text, chunk_id, "
                    "excerpt, verified FROM review_opinion_citations "
                    "WHERE opinion_id = ANY(%s) ORDER BY id ASC",
                    (list(opinions.keys()),),
                )
                for row in cur.fetchall():
                    cit = _row_to_review_opinion_citation(row)
                    if cit.opinion_id in opinions:
                        opinions[cit.opinion_id].citations.append(cit)
        return list(opinions.values())

    @staticmethod
    def upsert_clause_risk_assessment(
        *,
        contract_id: int,
        clause_db_id: int,
        risk_level: str,
        rationale: str,
        affected_party: str,
        confidence: float,
    ) -> ClauseRiskAssessmentRecord:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO clause_risk_assessments(contract_id, clause_id_ref, risk_level, "
                "rationale, affected_party, confidence) "
                "VALUES(%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (contract_id, clause_id_ref) DO UPDATE SET "
                "risk_level = EXCLUDED.risk_level, rationale = EXCLUDED.rationale, "
                "affected_party = EXCLUDED.affected_party, confidence = EXCLUDED.confidence "
                "RETURNING id, contract_id, clause_id_ref, risk_level, rationale, "
                "affected_party, confidence, created_at",
                (
                    contract_id,
                    clause_db_id,
                    risk_level.lower(),
                    rationale,
                    affected_party,
                    float(confidence),
                ),
            )
            return _row_to_clause_risk_assessment(cur.fetchone())

    @staticmethod
    def list_clause_risk_assessments(contract_id: int) -> list[ClauseRiskAssessmentRecord]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, contract_id, clause_id_ref, risk_level, rationale, affected_party, "
                "confidence, created_at FROM clause_risk_assessments "
                "WHERE contract_id = %s ORDER BY id ASC",
                (contract_id,),
            )
            return [_row_to_clause_risk_assessment(row) for row in cur.fetchall()]

    @staticmethod
    def insert_consistency_fact(
        *,
        contract_id: int,
        clause_db_id: int,
        category: str,
        fact_key: str,
        party: str,
        value_text: str,
        normalized_value: str,
        span_text: str,
        related_text: str,
        confidence: float,
    ) -> ConsistencyFactRecord:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO contract_consistency_facts(contract_id, clause_id_ref, category, "
                "fact_key, party, value_text, normalized_value, span_text, related_text, confidence) "
                "VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id, contract_id, clause_id_ref, category, fact_key, party, "
                "value_text, normalized_value, span_text, related_text, confidence, created_at",
                (
                    contract_id,
                    clause_db_id,
                    category,
                    fact_key,
                    party,
                    value_text,
                    normalized_value,
                    span_text,
                    related_text,
                    float(confidence),
                ),
            )
            return _row_to_consistency_fact(cur.fetchone())

    @staticmethod
    def list_consistency_facts(contract_id: int) -> list[ConsistencyFactRecord]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, contract_id, clause_id_ref, category, fact_key, party, value_text, "
                "normalized_value, span_text, related_text, confidence, created_at "
                "FROM contract_consistency_facts WHERE contract_id = %s ORDER BY id ASC",
                (contract_id,),
            )
            return [_row_to_consistency_fact(row) for row in cur.fetchall()]

    @staticmethod
    def insert_consistency_opinion(
        *,
        contract_id: int,
        opinion_type: str,
        review_dimension: str,
        finding: str,
        recommendation: str,
        related_clause_ids: list[str],
        evidence_facts: list[str],
        confidence: float,
    ) -> ConsistencyOpinionRecord:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO contract_consistency_opinions(contract_id, opinion_type, "
                "review_dimension, finding, recommendation, related_clause_ids_json, "
                "evidence_facts_json, confidence) "
                "VALUES(%s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id, contract_id, opinion_type, review_dimension, finding, "
                "recommendation, related_clause_ids_json, evidence_facts_json, confidence, created_at",
                (
                    contract_id,
                    opinion_type,
                    review_dimension,
                    finding,
                    recommendation,
                    json.dumps(related_clause_ids, ensure_ascii=False),
                    json.dumps(evidence_facts, ensure_ascii=False),
                    float(confidence),
                ),
            )
            return _row_to_consistency_opinion(cur.fetchone())

    @staticmethod
    def list_consistency_opinions(contract_id: int) -> list[ConsistencyOpinionRecord]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, contract_id, opinion_type, review_dimension, finding, "
                "recommendation, related_clause_ids_json, evidence_facts_json, confidence, created_at "
                "FROM contract_consistency_opinions WHERE contract_id = %s ORDER BY id ASC",
                (contract_id,),
            )
            return [_row_to_consistency_opinion(row) for row in cur.fetchall()]

    @staticmethod
    def upsert_consistency_risk_assessment(
        *,
        contract_id: int,
        risk_level: str,
        rationale: str,
        affected_party: str,
        confidence: float,
    ) -> ConsistencyRiskAssessmentRecord:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO contract_consistency_risk_assessments(contract_id, risk_level, "
                "rationale, affected_party, confidence) VALUES(%s, %s, %s, %s, %s) "
                "ON CONFLICT (contract_id) DO UPDATE SET risk_level = EXCLUDED.risk_level, "
                "rationale = EXCLUDED.rationale, affected_party = EXCLUDED.affected_party, "
                "confidence = EXCLUDED.confidence "
                "RETURNING id, contract_id, risk_level, rationale, affected_party, confidence, created_at",
                (
                    contract_id,
                    risk_level.lower(),
                    rationale,
                    affected_party,
                    float(confidence),
                ),
            )
            return _row_to_consistency_risk_assessment(cur.fetchone())

    @staticmethod
    def get_consistency_risk_assessment(
        contract_id: int,
    ) -> Optional[ConsistencyRiskAssessmentRecord]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, contract_id, risk_level, rationale, affected_party, confidence, created_at "
                "FROM contract_consistency_risk_assessments WHERE contract_id = %s",
                (contract_id,),
            )
            row = cur.fetchone()
        return _row_to_consistency_risk_assessment(row) if row else None


__all__ = [
    "ContractStore",
    "ContractRecord",
    "ClauseRecord",
    "RiskItemRecord",
    "RiskCitationRecord",
    "ReviewOpinionRecord",
    "ReviewOpinionCitationRecord",
    "ClauseRiskAssessmentRecord",
    "ConsistencyFactRecord",
    "ConsistencyOpinionRecord",
    "ConsistencyRiskAssessmentRecord",
]
