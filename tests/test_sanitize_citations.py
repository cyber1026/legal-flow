"""审查引用核验 `_sanitize_citations` 的单测（新范式：按 (law_name, article_no) 核验）。

校验三态：命中已核实集合 → verified=True 且回填；未命中 → verified=False 且保留不丢；
全空 → 丢弃。另含归一化匹配、法名简称兜底，以及 _collect_verified_articles 建表、
submit_review 结果解析等。
"""

from __future__ import annotations

import pytest

from app.contracts.review_agent import (
    _coerce_review_output,
    _collect_verified_articles,
    _extract_submitted_review,
    _sanitize_citations,
)
from app.contracts.prompts.risk_schema import ReviewCitation


def _review_payload(*, has_opinion: bool = False, opinions: list[dict] | None = None, note: str = ""):
    return {
        "has_opinion": has_opinion,
        "opinions": opinions or [],
        "risk_assessment": {
            "risk_level": "medium" if has_opinion else "none",
            "rationale": "存在需关注事项" if has_opinion else "未发现明显风险",
            "affected_party": "甲方" if has_opinion else "不适用",
            "confidence": 0.8,
        },
        "consistency_facts": [],
        "note": note,
    }


def _cite(*, law_name: str, article_no: str, chunk_id: str, **extra) -> dict:
    """构造一条 verify/search 工具 artifact 里的 citation。"""
    return {
        "index": extra.get("index", 1),
        "law_name": law_name,
        "article_no": article_no,
        "chunk_id": chunk_id,
        "citation_text": extra.get("citation_text", f"《{law_name}》{article_no}"),
        "content": extra.get("content", "法条原文……"),
    }


def _verified(*cites: dict) -> dict[tuple[str, str], dict]:
    """把若干工具 citation 收成「已核实集合」（复刻 _collect_verified_articles 的键）。"""
    from app.retrieval.article_no import normalize_article_no
    return {
        ((c["law_name"] or "").strip(), normalize_article_no(c["article_no"])): c
        for c in cites
    }


# ── 三态 ────────────────────────────────────────────────────────────────

def test_命中回填并标已核实():
    verified = _verified(_cite(
        law_name="中华人民共和国民法典", article_no="第五百三十三条",
        chunk_id="real-uuid", citation_text="《民法典》第五百三十三条",
        content="情势变更：合同成立后……",
    ))
    raw = [ReviewCitation(law_name="中华人民共和国民法典", article_no="第五百三十三条")]

    out = _sanitize_citations(raw, verified)

    assert len(out) == 1
    c = out[0]
    assert c.verified is True
    assert c.chunk_id == "real-uuid"
    assert c.article_no == "第五百三十三条"
    assert c.citation_text == "《民法典》第五百三十三条"
    assert c.excerpt == "情势变更：合同成立后……"  # 模型没填 excerpt，截 content 回填


def test_归一化命中():
    """模型写阿拉伯「第533条」，法库存中文「第五百三十三条」，应归一化后命中。"""
    verified = _verified(_cite(
        law_name="中华人民共和国民法典", article_no="第五百三十三条", chunk_id="x",
    ))
    raw = [ReviewCitation(law_name="中华人民共和国民法典", article_no="第533条")]

    out = _sanitize_citations(raw, verified)

    assert len(out) == 1 and out[0].verified is True and out[0].chunk_id == "x"


def test_法名简称兜底命中():
    """模型法名用简称「民法典」，法库全称「中华人民共和国民法典」，唯一候选时兜底命中。"""
    verified = _verified(_cite(
        law_name="中华人民共和国民法典", article_no="第五百三十三条", chunk_id="x",
    ))
    raw = [ReviewCitation(law_name="民法典", article_no="第五百三十三条")]

    out = _sanitize_citations(raw, verified)

    assert len(out) == 1 and out[0].verified is True
    assert out[0].law_name == "中华人民共和国民法典"  # 回填为法库全称


def test_未命中保留并标未核实():
    """模型引了一条法库没核实到的法条 → 保留、verified=False、chunk_id 空，不丢弃。"""
    verified = _verified(_cite(
        law_name="中华人民共和国民法典", article_no="第五百三十三条", chunk_id="x",
    ))
    raw = [ReviewCitation(
        law_name="中华人民共和国消费者权益保护法", article_no="第二十一条",
        citation_text="《消费者权益保护法》第二十一条",
    )]

    out = _sanitize_citations(raw, verified)

    assert len(out) == 1
    c = out[0]
    assert c.verified is False
    assert c.chunk_id == ""
    assert c.article_no == "第二十一条"
    assert c.law_name == "中华人民共和国消费者权益保护法"
    assert c.citation_text == "《消费者权益保护法》第二十一条"  # 保留模型自填


def test_跨法律同条号不误配():
    """已核实集合里两部法律都有同一条号，简称无法消歧时不应误配（多候选丢弃兜底）。"""
    verified = _verified(
        _cite(law_name="中华人民共和国民法典", article_no="第四条", chunk_id="a", index=1),
        _cite(law_name="中华人民共和国劳动法", article_no="第四条", chunk_id="b", index=2),
    )
    # 模型法名为空 + 条号第四条 → 两个候选，兼容性都通过 → 不唯一 → 不命中 → 标未核实
    raw = [ReviewCitation(law_name="", article_no="第四条")]

    out = _sanitize_citations(raw, verified)

    assert len(out) == 1 and out[0].verified is False


def test_全空丢弃():
    verified = _verified(_cite(law_name="X法", article_no="第十条", chunk_id="a"))
    raw = [ReviewCitation(law_name="", article_no="")]

    assert _sanitize_citations(raw, verified) == []


# ── _collect_verified_articles ───────────────────────────────────────────

def test_collect_verified_articles_合并多工具():
    """verify/search 多次调用的 artifact 应按归一化 (law_name, article_no) 合并成集合。"""
    from langchain_core.messages import ToolMessage

    msg1 = ToolMessage(
        content="x", tool_call_id="t1", name="verify_law_article",
        artifact={"citations": [_cite(
            law_name="中华人民共和国民法典", article_no="第五百三十三条", chunk_id="a")]},
    )
    msg2 = ToolMessage(
        content="y", tool_call_id="t2", name="search_law",
        artifact={"citations": [_cite(
            law_name="中华人民共和国劳动法", article_no="第二十条", chunk_id="b")]},
    )

    table = _collect_verified_articles([msg1, msg2])

    assert ("中华人民共和国民法典", "533") in table
    assert ("中华人民共和国劳动法", "20") in table
    assert table[("中华人民共和国民法典", "533")]["chunk_id"] == "a"


def test_collect_verified_articles_忽略其他工具():
    """非 verify/search 的 ToolMessage 不应进入已核实集合。"""
    from langchain_core.messages import ToolMessage

    msg = ToolMessage(
        content="审查结果已提交", tool_call_id="s1", name="submit_review",
        artifact={"review_output": _review_payload()},
    )
    assert _collect_verified_articles([msg]) == {}


# ── submit_review 结果解析（沿用，无 citation_index）────────────────────────

def test_extract_submitted_review_from_tool_artifact():
    from langchain_core.messages import ToolMessage

    msg = ToolMessage(
        content="审查结果已提交", tool_call_id="submit-1", name="submit_review",
        artifact={"review_output": _review_payload(note="未发现明显风险")},
    )
    out = _extract_submitted_review([msg])

    assert out is not None and out.has_opinion is False and out.note == "未发现明显风险"


def test_extract_submitted_review_from_ai_tool_call_args():
    from langchain_core.messages import AIMessage

    msg = AIMessage(
        content="",
        tool_calls=[{
            "name": "submit_review",
            "args": _review_payload(
                has_opinion=True,
                opinions=[{
                    "opinion_type": "提醒", "review_dimension": "权益明确性",
                    "finding": "条款约定不够明确。", "recommendation": "建议补充具体履行标准。",
                    "confidence": 0.8, "citations": [],
                }],
            ),
            "id": "submit-1",
        }],
    )
    out = _extract_submitted_review([msg])

    assert out is not None and out.has_opinion is True and len(out.opinions) == 1
    assert out.opinions[0].opinion_type == "提醒"
    assert out.opinions[0].review_dimension == "权益明确性"


def test_coerce_review_output_rejects_legacy_fields():
    with pytest.raises(Exception):
        _coerce_review_output({
            "has_risk": True,
            "risks": [{
                "opinion_type": "提醒", "review_dimension": "权益明确性", "risk_level": "medium",
                "risk_description": "条款约定不够明确。", "suggestion": "建议补充具体履行标准。",
                "confidence": 0.8, "citations": [],
            }],
            "note": "",
        })


def test_extract_submitted_review_returns_none_when_missing():
    from langchain_core.messages import AIMessage

    assert _extract_submitted_review([AIMessage(content="普通文本")]) is None
