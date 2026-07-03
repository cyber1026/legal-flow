"""合同审查上下文构建。

聊天入口只注入一行合同摘要，细节由 Supervisor 通过合同工具按需读取。
这样避免每轮都把完整审查报告塞进上下文。
"""

from __future__ import annotations

from app.contracts.store import ContractRecord, ContractStore


def build_contract_context(contract: ContractRecord) -> str:
    """Build a compact context hint; detailed data stays behind tools."""
    clauses = ContractStore.list_clauses(contract.id)
    opinions = ContractStore.list_review_opinions(contract.id)
    assessments = ContractStore.list_clause_risk_assessments(contract.id)
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "none": 0}
    for item in assessments:
        counts[item.risk_level] = counts.get(item.risk_level, 0) + 1
    return (
        f"本会话关联合同《{contract.title or contract.filename}》"
        f"（contract_id 由系统注入）：共 {len(clauses)} 条款 / {len(opinions)} 条审查意见；"
        f"严重 {counts.get('critical', 0)}、高危 {counts.get('high', 0)}、"
        f"中危 {counts.get('medium', 0)}、低危 {counts.get('low', 0)}、"
        f"无风险 {counts.get('none', 0)}。"
        "如需细节，请使用 list_clauses、get_clause、get_opinions、get_clause_risk_assessments 等工具读取。"
    )


__all__ = [
    "build_contract_context",
]
