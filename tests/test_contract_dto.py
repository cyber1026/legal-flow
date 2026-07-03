"""ReviewOpinionDTO + ClauseRiskAssessmentDTO 测试。"""
from __future__ import annotations

from datetime import datetime, timezone

from app.api.schemas.contract import ClauseRiskAssessmentDTO, ReviewOpinionDTO


def test_opinion_dto_accepts_opinion_fields():
    dto = ReviewOpinionDTO(
        id=1,
        clause_id_ref=100,
        opinion_type="说明",
        review_dimension="表述精确性",
        finding="d",
        recommendation="s",
        confidence=0.4,
        citations=[],
        created_at=datetime(2026, 5, 31, tzinfo=timezone.utc),
    )
    assert dto.opinion_type == "说明"
    assert dto.review_dimension == "表述精确性"
    assert dto.finding == "d"


def test_clause_risk_assessment_dto_accepts_none():
    dto = ClauseRiskAssessmentDTO(
        id=1,
        clause_id_ref=100,
        risk_level="none",
        rationale="无风险",
        affected_party="不适用",
        confidence=0.9,
        created_at=datetime(2026, 5, 31, tzinfo=timezone.utc),
    )
    assert dto.risk_level == "none"
