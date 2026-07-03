"""审查结束后，触发 supervisor 在会话内生成结构化总览报告的内部指令。

结构参照律协《合同审查业务操作指引》第8/9条。
"""

from __future__ import annotations


def build_overview_instruction(party_stance: str = "未知") -> str:
    """生成结构化总览指令；按委托人立场调整口径。"""
    if party_stance in ("甲方", "乙方", "中立"):
        stance_line = f"本次审查的委托人立场为「{party_stance}」，总览须站在该方角度评述。"
    else:
        stance_line = "本次审查委托人立场未知，按中立口径评述。"
    return (
        "系统内部任务：本会话的合同全量审查已完成。"
        f"{stance_line}"
        "请依次调用 get_opinions、get_clause_risk_assessments、get_consistency_opinions、"
        "get_consistency_risk_assessment 读取结构化结果，生成一份中文 Markdown 总览报告，"
        "严格包含以下小节：\n"
        "1. **总体结论**：合同是否有效、整体利益倾向性（偏向哪一方）。\n"
        "2. **条款级风险分布**：按无风险/低危/中危/高危/严重统计条款级风险评估 + 关键风险摘要。\n"
        "3. **主要审查意见**：汇总疑问/说明/提醒/建议/警告，不要把意见类型当作风险等级。\n"
        "4. **合同一致性审查**：基于一致性审查意见与一致性风险评估，说明前后冲突、缺失或需核实事项。\n"
        "5. **委托人利益保护**：站在委托人立场，汇总「对我方不利」的要点。\n"
        "6. **必须修改 / 重点关注条款清单**：按优先级排序。\n"
        "7. **附随告知**：本审查仅对来稿负责；标的/价格等商务条款由委托人自行决定。\n\n"
        "只能基于上述工具返回的结构化结果，不要编造未出现的内容；"
        "若无任何意见，请明确给出『未发现显著合同风险』的结论。"
    )


CONTRACT_OVERVIEW_INSTRUCTION = build_overview_instruction()

__all__ = ["build_overview_instruction", "CONTRACT_OVERVIEW_INSTRUCTION"]
