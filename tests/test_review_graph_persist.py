"""review_graph 审查意见序列化测试。"""
from __future__ import annotations

from datetime import datetime, timezone

from app.contracts.review_graph import _opinion_to_dict
from app.contracts.store import ClauseRecord, ReviewOpinionRecord


def _clause():
    return ClauseRecord(
        id=100, contract_id=10, clause_id="c1", section_path="一",
        clause_no="第1条", title="标的", text="...", page_no=1, bbox=None,
        chunk_index=0, review_status="done", review_has_risk=True, reasoning=[],
    )


def test_opinion_to_dict_fields():
    rec = ReviewOpinionRecord(
        id=1, contract_id=10, clause_id_ref=100,
        opinion_type="警告", review_dimension="内容合法性",
        finding="d", recommendation="s", confidence=0.9,
        created_at=datetime(2026, 5, 31, tzinfo=timezone.utc),
    )
    d = _opinion_to_dict(rec, _clause())
    assert d["opinion_type"] == "警告"
    assert d["review_dimension"] == "内容合法性"
    assert d["finding"] == "d"
    assert d["clause_id"] == "c1"
    assert "risk_type" not in d
    assert "risk_level" not in d
