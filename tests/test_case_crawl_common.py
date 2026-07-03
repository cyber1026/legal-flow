"""案例抓取公共逻辑单测。"""

from __future__ import annotations

import os
import sys

# 爬虫脚本用扁平 import，需把 scripts/crawl 加入 sys.path。
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts", "crawl")))

from case_crawl_common import classify_case_contract_relevance, extract_cause_of_action


def test_extract_cause_of_action_不把自然人尾字并入案由():
    title = "陈某芬诉罗某、何某琴民间借贷纠纷案"

    assert extract_cause_of_action(title) == "民间借贷纠纷"


def test_extract_cause_of_action_处理等人与多被告残留():
    assert extract_cause_of_action("王某诉吉林某景健身公司、张某娇等民间借贷纠纷案") == "民间借贷纠纷"
    assert extract_cause_of_action("顾某萍与王某宝等人民间借贷纠纷案") == "民间借贷纠纷"


def test_classify_case_contract_relevance_使用修正后案由():
    row = {
        "doc_title": "陈某芬诉罗某、何某琴民间借贷纠纷案",
        "keywords_text": "民事/借款合同/民间借贷/夫妻共同债务",
    }

    out = classify_case_contract_relevance(row)

    assert out["contract_related"] is True
    assert out["contract_priority"] == "P0_CAUSE"
    assert out["cause_of_action"] == "民间借贷纠纷"
    assert out["classify_reason"] == "案由属合同/准合同类：民间借贷纠纷"
