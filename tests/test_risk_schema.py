"""三轴审查意见 schema 单元测试。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.contracts.prompts.risk_schema import (
    ClauseRiskAssessment,
    OpinionType,
    ReviewOpinion,
    ReviewDimension,
    RiskLevel,
    ReviewOutput,
)


def test_review_opinion_accepts_two_axes_without_risk_level():
    item = ReviewOpinion(
        opinion_type="警告",
        review_dimension="内容合法性",
        finding="x",
        recommendation="y",
    )
    assert item.opinion_type == "警告"
    assert item.review_dimension == "内容合法性"
    assert not hasattr(item, "risk_level")
    # risk_type 字段已移除
    assert not hasattr(item, "risk_type")


def test_clause_risk_assessment_allows_none():
    item = ClauseRiskAssessment(
        risk_level="none",
        rationale="无风险",
        affected_party="不适用",
        confidence=0.9,
    )
    assert item.risk_level == "none"


def test_invalid_opinion_type_rejected():
    with pytest.raises(ValidationError):
        ReviewOpinion(
            opinion_type="不存在的类型",
            review_dimension="内容合法性",
            finding="x",
            recommendation="y",
        )


def test_review_output_defaults():
    out = ReviewOutput(
        has_opinion=False,
        risk_assessment=ClauseRiskAssessment(
            risk_level="none", rationale="无意见", affected_party="不适用", confidence=1.0
        ),
    )
    assert out.opinions == []
    assert out.note == ""
