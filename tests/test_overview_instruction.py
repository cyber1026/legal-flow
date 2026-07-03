"""结构化总览指令测试。"""
from __future__ import annotations

from app.contracts.prompts.overview_instruction import build_overview_instruction


def test_overview_has_structure_and_stance():
    text = build_overview_instruction(party_stance="甲方")
    for kw in [
        "利益倾向",
        "条款级风险",
        "主要审查意见",
        "合同一致性审查",
        "委托人利益",
        "修改",
        "get_opinions",
        "get_clause_risk_assessments",
        "get_consistency_opinions",
        "get_consistency_risk_assessment",
    ]:
        assert kw in text
    assert "甲方" in text


def test_overview_neutral_when_unknown():
    text = build_overview_instruction(party_stance="未知")
    assert "get_opinions" in text
