"""合同风险评测的 Markdown 抽取与规则过滤单测。"""

from __future__ import annotations

from pathlib import Path

from app.contracts.clause_splitter import Clause
from eval.contract_clause_risk_review.filters import rule_filter_clause
from eval.contract_clause_risk_review.markdown_loader import extract_contract_body, split_standard_contract


def test_markdown_body_extraction_skips_risk_prompt(tmp_path: Path) -> None:
    """Markdown 抽取应跳过 `---` 前的风险提示和元信息。"""
    path = tmp_path / "测试买卖合同.md"
    path.write_text(
        """# 测试买卖合同

## 风险提示

这里是示范文本自带风险提示，不应进入评测条款。

---

<h2 align="center">测试买卖合同</h2>

**第一条**　付款方式

买受人应在收到货物并验收合格后三十日内支付全部货款；逾期付款的，应按照未付款金额每日万分之五向出卖人支付违约金。
""",
        encoding="utf-8",
    )

    body = extract_contract_body(path.read_text(encoding="utf-8"))
    clauses = split_standard_contract(path)

    assert "风险提示" not in body
    assert len(clauses) == 1
    assert clauses[0].clause_no == "第一条"
    assert "逾期付款" in clauses[0].text


def test_rule_filter_rejects_meaningless_category_clause() -> None:
    """纯定义/分类说明条款不应作为风险评测 seed。"""
    clause = Clause(
        clause_id="c1",
        clause_no="第一条",
        title="骑行卡种类",
        section_path="",
        text="第一条　骑行卡种类\n骑行卡包括单车骑行时长卡和单车骑行次卡两大类。",
    )

    result = rule_filter_clause(clause)

    assert not result.passed
    assert "pure_definition_or_category" in result.reasons or "too_short" in result.reasons


def test_rule_filter_keeps_meaningful_payment_clause() -> None:
    """付款和违约责任条款应被识别为有评测价值。"""
    clause = Clause(
        clause_id="c1",
        clause_no="第二条",
        title="付款方式及违约责任",
        section_path="",
        text=(
            "买受人应在收到货物并验收合格后三十日内向出卖人支付全部货款。"
            "买受人逾期付款超过十日的，应按照未付款金额每日万分之五支付违约金；"
            "逾期超过三十日的，出卖人有权解除合同并要求赔偿损失。"
        ),
    )

    result = rule_filter_clause(clause)

    assert result.passed
    assert "付款价款" in result.dimensions
    assert "违约解除" in result.dimensions


def test_markdown_loader_splits_inline_articles_and_fullwidth_numbering(tmp_path: Path) -> None:
    """同一行多个条款、中文顿号编号和全角数字编号应被拆开。"""
    path = tmp_path / "表格式合同.md"
    path.write_text(
        """---

<h2 align="center">表格式合同</h2>

**第二条**　质量标准： 第三条 出卖人对质量负责的期限：

一、验收标准、方法及提出异议的期限： 二、结算方式及期限：

（注：空格如不够用，可以另接） 三、质量和数量验收标准及方法： 四、货款、运杂费结算方式及结算期限：

1．本合同按有关规定执行。 ｜ 8．如需提供担保，另立合同担保书，作为合同附件。
""",
        encoding="utf-8",
    )

    clauses = split_standard_contract(path)
    clause_nos = [clause.clause_no for clause in clauses]

    assert "第二条" in clause_nos
    assert "第三条" in clause_nos
    assert "一" in clause_nos
    assert "二" in clause_nos
    assert "三" in clause_nos
    assert "四" in clause_nos
    assert "1" in clause_nos
    assert "8" in clause_nos


def test_markdown_loader_extracts_legal_table_fields(tmp_path: Path) -> None:
    """表格里的法律字段应被提升为独立候选条款。"""
    path = tmp_path / "表格字段合同.md"
    path.write_text(
        """---

<h2 align="center">表格字段合同</h2>

| 违约责任 | 出卖人不能交货的，应向买受人偿付不能交货部分货款百分之五的违约金。 | 鉴证意见 | 空 |
| --- | --- | --- | --- |
| 争议解决方式 | 本合同在履行过程中发生的争议，由双方协商解决；协商不成的，依法向人民法院起诉。 |  |  |
""",
        encoding="utf-8",
    )

    clauses = split_standard_contract(path)
    texts = [clause.text for clause in clauses]

    assert any("违约责任" in text and "不能交货" in text for text in texts)
    assert any("争议解决方式" in text and "人民法院" in text for text in texts)
