"""Review Agent 的结构化输出 Schema（Pydantic）。

合同审查 Agent 通过 submit_review 工具提交后，再校验并转换为 ReviewOutput。
审查意见与风险评估解耦：
- opinion_type 意见类型（指引 5.6）：疑问/说明/提醒/建议/警告
- review_dimension 审查维度（指引第6条）：主体合格性/内容合法性/条款实用性/权益明确性/合同严谨性/表述精确性
- risk_assessment 条款级综合风险评估：none/low/medium/high/critical
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# 意见类型枚举（指引 5.6 五类意见）
OpinionType = Literal["疑问", "说明", "提醒", "建议", "警告"]

# 审查维度枚举（指引第6条 六大维度）
ReviewDimension = Literal[
    "主体合格性",
    "内容合法性",
    "条款实用性",
    "权益明确性",
    "合同严谨性",
    "表述精确性",
]

# 风险等级 5 档（与 PG check 约束一致，统一小写英文；none 对应「无风险」）
RiskLevel = Literal["none", "low", "medium", "high", "critical"]

AffectedParty = Literal["甲方", "乙方", "双方", "不适用", "未知"]
ConsistencyParty = Literal["甲方", "乙方", "双方", "第三方", "不适用", "未知"]
ConsistencyCategory = Literal[
    "party_identity",
    "defined_term",
    "amount",
    "payment",
    "time_deadline",
    "obligation",
    "condition",
    "liability",
    "termination",
    "notice",
    "jurisdiction",
    "attachment_reference",
    "clause_reference",
    "other",
]


class ReviewCitation(BaseModel):
    """审查意见引用的法条。后端按 (law_name, article_no) 在「已核实集合」里核验并回填溯源字段。"""

    law_name: str = Field(default="", description="法律全称，如「中华人民共和国民法典」")
    article_no: str = Field(default="", description="条文号，如「第四百九十七条」")
    citation_text: str = Field(default="", description="标准引用文本，如「《民法典》第497条」")
    excerpt: str = Field(default="", description="原文摘录，截取最相关的一两句")
    chunk_id: str = Field(default="", description="系统按 (law_name, article_no) 回填，无需填写")
    verified: bool = Field(
        default=False,
        description="是否在本地法库核实到该条文（系统判定，无需填写）",
    )


class ReviewOpinion(BaseModel):
    """单条审查意见：只表达意见内容，不承载风险等级。"""

    opinion_type: OpinionType = Field(description="意见类型，从枚举中选一个")
    review_dimension: ReviewDimension = Field(description="审查维度，从枚举中选一个")
    finding: str = Field(description="审查发现：指出什么问题、疑问或说明事项")
    recommendation: str = Field(description="处理建议：如何修改、补充、核验或谈判")
    confidence: float = Field(
        default=0.7, ge=0.0, le=1.0, description="判断置信度 0~1"
    )
    citations: list[ReviewCitation] = Field(
        default_factory=list,
        description="支撑该意见的法律依据，可为空但应尽量给出",
    )


class ClauseRiskAssessment(BaseModel):
    """条款级综合风险评估；与单条意见解耦。"""

    risk_level: RiskLevel = Field(description="条款整体风险等级")
    rationale: str = Field(description="综合整个条款后的风险评级理由")
    affected_party: AffectedParty = Field(description="主要受影响的一方")
    confidence: float = Field(
        default=0.7, ge=0.0, le=1.0, description="判断置信度 0~1"
    )


class ConsistencyFact(BaseModel):
    """单条款中可供全合同一致性审查横向比对的事实。"""

    category: ConsistencyCategory
    key: str = Field(description="事实名称，如甲方名称、付款期限、违约责任触发条件")
    party: ConsistencyParty
    value_text: str = Field(description="条款原文中的事实值")
    normalized_value: str = Field(description="规范化后的值，用于跨条款比较")
    span_text: str = Field(description="支撑该事实的最小原文片段")
    related_text: str = Field(default="", description="条件、例外、触发场景等上下文")
    confidence: float = Field(
        default=0.7, ge=0.0, le=1.0, description="事实抽取置信度 0~1"
    )


class ReviewOutput(BaseModel):
    """Review Agent 的最终结构化输出。"""

    has_opinion: bool = Field(description="是否产出任何审查意见")
    opinions: list[ReviewOpinion] = Field(
        default_factory=list,
        description="审查意见列表，has_opinion=False 时为空数组",
    )
    risk_assessment: ClauseRiskAssessment = Field(description="条款级综合风险评估")
    consistency_facts: list[ConsistencyFact] = Field(
        default_factory=list,
        description="供合同级一致性审查使用的结构化事实",
    )
    note: str = Field(default="", description="补充说明（例如信息不足时的解释），无则空串")


class ConsistencyOpinion(BaseModel):
    """合同级一致性审查意见；不承载风险等级。"""

    opinion_type: OpinionType
    review_dimension: ReviewDimension
    finding: str
    recommendation: str
    related_clause_ids: list[str] = Field(default_factory=list)
    # 支撑该意见的一致性事实摘要（每条一句话）。用 list[str] 而非 list[dict]：
    # 后者在 DeepSeek strict 模式下会生成「无属性 object」被 API 拒（400），见 consistency_agent。
    evidence_facts: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)


class ContractConsistencyRiskAssessment(BaseModel):
    """合同一致性层面的整体风险评估。"""

    risk_level: RiskLevel
    rationale: str
    affected_party: AffectedParty
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)


class ConsistencyReviewOutput(BaseModel):
    """一致性审查节点最终结构化输出。"""

    has_opinion: bool
    opinions: list[ConsistencyOpinion] = Field(default_factory=list)
    risk_assessment: ContractConsistencyRiskAssessment
    note: str = ""


__all__ = [
    "ReviewOutput",
    "ReviewOpinion",
    "ReviewCitation",
    "ClauseRiskAssessment",
    "ConsistencyFact",
    "ConsistencyOpinion",
    "ContractConsistencyRiskAssessment",
    "ConsistencyReviewOutput",
    "OpinionType",
    "ReviewDimension",
    "RiskLevel",
    "AffectedParty",
    "ConsistencyParty",
    "ConsistencyCategory",
]
