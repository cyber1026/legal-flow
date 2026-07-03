"""ClauseSplitter 与 ParsedDoc 中间表示的最小单测。

不依赖 docling/PaddleOCR，纯 Python 跑得动。
"""

from __future__ import annotations

from app.contracts.clause_splitter import split_clauses
from app.contracts.parser.base import ParsedBlock, ParsedDoc


def _make_doc(blocks: list[ParsedBlock]) -> ParsedDoc:
    return ParsedDoc(title="测试合同", blocks=blocks, doc_type="docx")


def test_split_by_article_no():
    """显式「第X条」编号应正确切分为多条。"""
    doc = _make_doc(
        [
            ParsedBlock(text="第一条 甲方为出租人，乙方为承租人。", page_no=1, bbox=[0, 0, 100, 20]),
            ParsedBlock(text="补充说明：本合同自签订之日起生效。", page_no=1, bbox=[0, 20, 100, 40]),
            ParsedBlock(text="第二条 租赁期限为一年。", page_no=1, bbox=[0, 40, 100, 60]),
        ]
    )
    clauses = split_clauses(doc)
    assert len(clauses) == 2
    assert clauses[0].clause_no == "第一条"
    assert "出租人" in clauses[0].text
    assert "本合同自签订之日起生效" in clauses[0].text  # 续行被合并
    assert clauses[1].clause_no == "第二条"
    assert clauses[1].text.startswith("租赁期限")


def test_section_path_propagates():
    """章节标题应反映在 clause.section_path。"""
    doc = _make_doc(
        [
            ParsedBlock(text="第一章 总则", block_type="heading"),
            ParsedBlock(text="第一条 甲乙双方按本合同执行。"),
            ParsedBlock(text="第二章 租金"),
            ParsedBlock(text="第二条 月租金 5000 元。"),
        ]
    )
    clauses = split_clauses(doc)
    assert len(clauses) == 2
    assert "第一章" in clauses[0].section_path
    assert "第二章" in clauses[1].section_path


def test_numeric_outline_split():
    """1./1.1/1.2 这种条目也能被识别。"""
    doc = _make_doc(
        [
            ParsedBlock(text="1. 双方同意保密。"),
            ParsedBlock(text="保密期限五年。"),
            ParsedBlock(text="2. 违约金为合同总额 20%。"),
        ]
    )
    clauses = split_clauses(doc)
    assert len(clauses) == 2
    assert clauses[0].clause_no == "1"
    assert "保密期限五年" in clauses[0].text
    assert clauses[1].clause_no == "2"


def test_numeric_subitems_stay_inside_cn_article():
    """中文第X条内部的数字子项不应被提升为同级主条款。"""
    doc = _make_doc(
        [
            ParsedBlock(text="第三条 双方的权利和义务"),
            ParsedBlock(text="1、乙方按照本合同约定提供服务，有权请求甲方支付相关业务委托费。"),
            ParsedBlock(text="2. 甲方提供展示所需企业标识（LOGO）或企业名称字体及格式。"),
            ParsedBlock(text="3) 甲乙双方同意，本合同严格遵守中华人民共和国法律。"),
            ParsedBlock(text="第四条 保密义务"),
            ParsedBlock(text="双方应对合作中知悉的商业秘密承担保密义务。"),
        ]
    )
    clauses = split_clauses(doc)
    assert len(clauses) == 2
    assert clauses[0].clause_no == "第三条"
    assert clauses[0].title == "双方的权利和义务"
    assert "1、乙方按照本合同约定提供服务" in clauses[0].text
    assert "2. 甲方提供展示所需企业标识" in clauses[0].text
    assert "3) 甲乙双方同意" in clauses[0].text
    assert clauses[1].clause_no == "第四条"


def test_bbox_union_and_page():
    """单条款多行的 bbox 应合并、page_no 取首个非 None。"""
    doc = _make_doc(
        [
            ParsedBlock(text="第一条 甲方义务", page_no=2, bbox=[10, 10, 80, 30]),
            ParsedBlock(text="按时支付报酬。", page_no=2, bbox=[10, 30, 60, 50]),
        ]
    )
    clauses = split_clauses(doc)
    assert len(clauses) == 1
    c = clauses[0]
    assert c.page_no == 2
    assert c.bbox == [10.0, 10.0, 80.0, 50.0]


def test_fallback_split_when_no_marker():
    """完全无编号 + 无 heading 时走兜底，至少切出 1 条。"""
    doc = _make_doc(
        [ParsedBlock(text="这是一段普通正文，没有任何条款编号。", page_no=1)]
    )
    clauses = split_clauses(doc)
    assert len(clauses) >= 1
    assert "普通正文" in clauses[0].text
