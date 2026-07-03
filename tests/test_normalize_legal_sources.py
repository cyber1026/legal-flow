"""法律语料规范化逻辑单测。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

from normalize_legal_sources import clean_relevant_statutes, parse_citations


def test_parse_citations_保留连续条文和款项():
    text = (
        "《中华人民共和国民法典》第6条、第7条（本案适用的是2017年10月1日施行的"
        "《中华人民共和国民法总则》第6条、第7条）\n"
        " 《中华人民共和国民法典》第562条第2款、第566条、第577条、第585条"
        "（本案适用的是1999年10月1日施行的《中华人民共和国合同法》第93条第2款、第97条、107条、第114条）"
    )

    cites = parse_citations(text)

    assert {"law": "民法典", "article": "第6条"} in cites
    assert {"law": "民法典", "article": "第7条"} in cites
    assert {"law": "民法典", "article": "第562条第2款"} in cites
    assert {"law": "民法典", "article": "第566条"} in cites
    assert {"law": "民法典", "article": "第577条"} in cites
    assert {"law": "民法典", "article": "第585条"} in cites
    assert {"law": "合同法", "article": "第107条"} in cites


def test_clean_relevant_statutes_去掉后一节裁判经过标题():
    text = (
        "《中华人民共和国民法典》第6条、第7条\n"
        " 《中华人民共和国民法典》第562条第2款、第566条、第577条、第585条\n\n"
        "###### 一审：上海市静安区人民法院（2018）沪0106民初7903号民事判决（2019年8月30日）"
    )

    cleaned = clean_relevant_statutes(text)

    assert "第562条第2款" in cleaned
    assert "一审" not in cleaned
