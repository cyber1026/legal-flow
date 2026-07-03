"""store 风险行转换与记录 to_dict 测试（不连库）。"""
from __future__ import annotations

from datetime import datetime, timezone

from app.contracts.store import RiskItemRecord, _row_to_risk


def test_row_to_risk_reads_three_axes():
    row = {
        "id": 1,
        "contract_id": 10,
        "clause_id_ref": 100,
        "opinion_type": "警告",
        "review_dimension": "内容合法性",
        "risk_level": "HIGH",
        "description": "d",
        "suggestion": "s",
        "confidence": 0.9,
        "created_at": datetime(2026, 5, 31, tzinfo=timezone.utc),
    }
    rec = _row_to_risk(row)
    assert rec.opinion_type == "警告"
    assert rec.review_dimension == "内容合法性"
    assert rec.risk_level == "high"  # 归一小写
    assert not hasattr(rec, "risk_type")


def test_risk_record_to_dict_has_three_axes():
    rec = RiskItemRecord(
        id=1, contract_id=10, clause_id_ref=100,
        opinion_type="提醒", review_dimension="权益明确性", risk_level="medium",
        description="d", suggestion="s", confidence=0.5,
        created_at=datetime(2026, 5, 31, tzinfo=timezone.utc),
    )
    d = rec.to_dict()
    assert d["opinion_type"] == "提醒"
    assert d["review_dimension"] == "权益明确性"
    assert "risk_type" not in d
