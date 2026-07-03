"""合同审查工具文本渲染测试（打桩 store）。"""
from __future__ import annotations

from datetime import datetime, timezone

import app.agents.contract_tools as ct
from app.contracts.store import ClauseRecord, ClauseRiskAssessmentRecord, ContractRecord, ReviewOpinionRecord


def _contract():
    return ContractRecord(
        id=10, user_id=1, session_id="s", job_id="j", filename="f.pdf",
        mime="application/pdf", doc_type="pdf", storage_path="/x", title="T",
        status="done", parsed_clauses=1, risk_count=1, error=None,
        started_at=None, finished_at=None,
        created_at=datetime(2026, 5, 31, tzinfo=timezone.utc),
        party_stance="甲方",
    )


def _clause():
    return ClauseRecord(
        id=100, contract_id=10, clause_id="c1", section_path="一",
        clause_no="第1条", title="标的", text="...", page_no=1, bbox=None,
        chunk_index=0, review_status="done", review_has_risk=True, reasoning=[],
    )


def _opinion():
    return ReviewOpinionRecord(
        id=1, contract_id=10, clause_id_ref=100,
        opinion_type="警告", review_dimension="内容合法性",
        finding="单方免责", recommendation="改双向", confidence=0.9,
        created_at=datetime(2026, 5, 31, tzinfo=timezone.utc),
    )


def _assessment(level):
    return ClauseRiskAssessmentRecord(
        id=1, contract_id=10, clause_id_ref=100, risk_level=level,
        rationale="综合风险", affected_party="甲方", confidence=0.9,
        created_at=datetime(2026, 5, 31, tzinfo=timezone.utc),
    )


def test_format_opinion_shows_type_and_dimension(monkeypatch):
    monkeypatch.setattr(ct.ContractStore, "get_by_id", lambda cid: _contract())
    monkeypatch.setattr(ct.ContractStore, "list_review_opinions", lambda cid: [_opinion()])
    monkeypatch.setattr(ct.ContractStore, "list_clauses", lambda cid: [_clause()])
    out = ct.get_opinions.func({"contract_id": 10})
    assert "警告" in out
    assert "内容合法性" in out


def test_none_level_filter_and_count(monkeypatch):
    monkeypatch.setattr(ct.ContractStore, "get_by_id", lambda cid: _contract())
    monkeypatch.setattr(
        ct.ContractStore,
        "list_clause_risk_assessments",
        lambda cid: [_assessment("none"), _assessment("high")],
    )
    monkeypatch.setattr(ct.ContractStore, "list_clauses", lambda cid: [_clause()])
    out = ct.get_clause_risk_assessments.func({"contract_id": 10})
    # 统计行包含「无风险」（none 档标签）计数；「提示」不是风险等级。
    assert "无风险" in out
    assert "提示" not in out
