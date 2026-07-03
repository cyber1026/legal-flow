"""审查系统提示词关键要素检查（防止重写时丢失指引要素）。"""
from __future__ import annotations

from app.contracts.prompts.review_system import REVIEW_SYSTEM_PROMPT as P


def test_prompt_has_three_axes():
    for kw in ["opinion_type", "review_dimension", "risk_level"]:
        assert kw in P
    # 五类意见
    for op in ["疑问", "说明", "提醒", "建议", "警告"]:
        assert op in P
    # 六维
    for dim in ["主体合格性", "内容合法性", "条款实用性", "权益明确性", "合同严谨性", "表述精确性"]:
        assert dim in P


def test_prompt_keeps_verification_paradigm():
    # 不强制基于检索；引用法条才核验
    assert "verify_law_article" in P
    assert "submit_review" in P
    # 不再出现废弃的 risk_type 枚举名
    assert "单方面免责" not in P


def test_prompt_has_stance_and_none_level():
    assert "委托人" in P
    assert "none" in P
    assert "无风险" in P
    assert "提示" not in P.partition("`risk_level`")[2].partition("`citations`")[0]
