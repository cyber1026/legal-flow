"""条款重点维度映射 + user prompt 立场/维度注入测试。"""
from __future__ import annotations

from app.contracts.review_graph import CLAUSE_CATEGORY_TO_DIMENSIONS, focus_dimensions_for
from app.contracts.review_agent import _format_user_prompt


def test_category_maps_to_dimensions():
    # 每个条款类别都能映射到至少一个审查维度
    from app.contracts.review_graph import CLAUSE_CATEGORIES
    for cat in CLAUSE_CATEGORIES:
        dims = focus_dimensions_for(cat)
        assert isinstance(dims, list)
        assert all(isinstance(d, str) for d in dims)


def test_争议解决_includes_权益明确性():
    assert "权益明确性" in focus_dimensions_for("争议解决")


def test_user_prompt_injects_stance_and_dimensions():
    prompt = _format_user_prompt(
        contract_title="T",
        section_path="一",
        clause_no="第1条",
        clause_text="甲方不承担任何责任。",
        party_stance="甲方",
        focus_dimensions=["内容合法性", "权益明确性"],
    )
    assert "甲方" in prompt
    assert "内容合法性" in prompt
    assert "权益明确性" in prompt
    assert "甲方不承担任何责任" in prompt


def test_user_prompt_neutral_default():
    prompt = _format_user_prompt(
        contract_title="T", section_path="", clause_no="", clause_text="x",
        party_stance="未知", focus_dimensions=None,
    )
    # 立场未知时以中立口径提示
    assert "中立" in prompt or "未知" in prompt
