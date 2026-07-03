"""submit_review strict 参数 → ReviewOutput 转换测试。"""
from __future__ import annotations

from app.contracts.review_agent import (
    ReviewSubmission,
    _coerce_review_output,
    _finalize_review,
)


def _payload():
    return {
        "has_opinion": True,
        "opinions": [
            {
                "opinion_type": "警告",
                "review_dimension": "内容合法性",
                "finding": "单方免责",
                "recommendation": "改为双向",
                "confidence": 0.8,
                "citations": [],
            }
        ],
        "risk_assessment": {
            "risk_level": "high",
            "rationale": "存在单方免责安排",
            "affected_party": "甲方",
            "confidence": 0.8,
        },
        "consistency_facts": [],
        "note": "",
    }


def test_coerce_keeps_opinion_and_clause_risk_assessment():
    out = _coerce_review_output(_payload())
    assert out.opinions[0].opinion_type == "警告"
    assert out.opinions[0].review_dimension == "内容合法性"
    assert out.risk_assessment.risk_level == "high"


def test_finalize_with_no_verified_keeps_unverified_citation():
    sub = _coerce_review_output(
        {
            "has_opinion": True,
            "opinions": [
                {
                    "opinion_type": "提醒",
                    "review_dimension": "权益明确性",
                    "finding": "d",
                    "recommendation": "s",
                    "confidence": 0.6,
                    "citations": [
                        {"law_name": "中华人民共和国民法典", "article_no": "第四百九十七条",
                         "citation_text": "《民法典》第497条", "excerpt": ""}
                    ],
                }
            ],
            "risk_assessment": {
                "risk_level": "medium",
                "rationale": "权益约定不明确",
                "affected_party": "甲方",
                "confidence": 0.7,
            },
            "consistency_facts": [],
            "note": "",
        }
    )
    final = _finalize_review(sub, verified={})
    assert final.opinions[0].citations[0].verified is False
    assert final.opinions[0].opinion_type == "提醒"
