"""合同审查相关的 API Pydantic DTO。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

RiskLevel = Literal["none", "low", "medium", "high", "critical"]


class ContractSummary(BaseModel):
    """合同列表/上传响应中使用的摘要视图。"""

    id: int
    session_id: Optional[str] = None
    job_id: str
    filename: str
    title: str
    doc_type: str
    status: str
    parsed_clauses: int = 0
    risk_count: int = 0
    opinion_count: int = 0
    error: Optional[str] = None
    party_stance: str = "未知"
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime


class ContractPartyStanceRequest(BaseModel):
    """设置合同委托人立场。"""

    party_stance: Literal["甲方", "乙方", "中立"]


class ContractClauseDTO(BaseModel):
    """单条款详情。"""

    id: int
    clause_id: str
    section_path: str
    clause_no: str
    title: str
    text: str
    page_no: Optional[int] = None
    bbox: Optional[list[float]] = None
    chunk_index: int = 0
    review_status: str = "pending"
    review_has_risk: bool = False
    review_has_opinion: bool = False
    reasoning: list[dict[str, Any]] = Field(default_factory=list)


class ReviewCitationDTO(BaseModel):
    """审查意见引用。"""

    law_name: str = ""
    article_no: str = ""
    citation_text: str = ""
    chunk_id: str = ""
    excerpt: str = ""
    verified: bool = False


class ReviewOpinionDTO(BaseModel):
    """审查意见条目；不包含风险等级。"""

    id: int
    clause_id_ref: int
    opinion_type: str
    review_dimension: str
    finding: str
    recommendation: str
    confidence: float = 0.0
    citations: list[ReviewCitationDTO] = Field(default_factory=list)
    created_at: datetime


class ClauseRiskAssessmentDTO(BaseModel):
    """条款级综合风险评估。"""

    id: int
    clause_id_ref: int
    risk_level: RiskLevel
    rationale: str
    affected_party: str
    confidence: float = 0.0
    created_at: datetime


class ConsistencyOpinionDTO(BaseModel):
    """合同一致性审查意见；不包含风险等级。"""

    id: int
    opinion_type: str
    review_dimension: str
    finding: str
    recommendation: str
    related_clause_ids: list[str] = Field(default_factory=list)
    evidence_facts: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    created_at: datetime


class ConsistencyRiskAssessmentDTO(BaseModel):
    """合同一致性层面的整体风险评估。"""

    id: int
    risk_level: RiskLevel
    rationale: str
    affected_party: str
    confidence: float = 0.0
    created_at: datetime


class ContractReport(BaseModel):
    """合同详情/审查报告，包含条款、意见、风险评估与一致性审查结果。"""

    contract: ContractSummary
    clauses: list[ContractClauseDTO]
    opinions: list[ReviewOpinionDTO]
    clause_risk_assessments: list[ClauseRiskAssessmentDTO] = Field(default_factory=list)
    consistency_opinions: list[ConsistencyOpinionDTO] = Field(default_factory=list)
    consistency_risk_assessment: Optional[ConsistencyRiskAssessmentDTO] = None


__all__ = [
    "ContractSummary",
    "ContractPartyStanceRequest",
    "ContractClauseDTO",
    "ReviewOpinionDTO",
    "ReviewCitationDTO",
    "ClauseRiskAssessmentDTO",
    "ConsistencyOpinionDTO",
    "ConsistencyRiskAssessmentDTO",
    "ContractReport",
]
