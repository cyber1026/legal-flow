"""合同风险评测的数据结构与标签映射。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

EvalLabel = Literal["safe", "medium", "high"]

SYSTEM_TO_EVAL_LABEL: dict[str, EvalLabel] = {
    "none": "safe",
    "low": "safe",
    "medium": "medium",
    "high": "high",
    "critical": "high",
}

LABEL_ORDER: dict[EvalLabel, int] = {"safe": 0, "medium": 1, "high": 2}
LABELS: tuple[EvalLabel, ...] = ("safe", "medium", "high")


@dataclass(slots=True)
class RuleFilterResult:
    """规则筛选结果，保留拒绝原因和可评测维度。"""

    passed: bool
    reasons: list[str] = field(default_factory=list)
    score: int = 0
    dimensions: list[str] = field(default_factory=list)
    placeholder_ratio: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """转换为可 JSON 序列化的字典。"""
        return asdict(self)


@dataclass(slots=True)
class CandidateClause:
    """从标准合同正文中抽取出的候选条款。"""

    candidate_id: str
    contract_name: str
    source_path: str
    source_index: int
    clause_id: str
    clause_no: str
    title: str
    section_path: str
    text: str
    rule_filter: RuleFilterResult
    llm_filter: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为可 JSON 序列化的字典。"""
        data = asdict(self)
        data["rule_filter"] = self.rule_filter.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CandidateClause":
        """从 JSON 字典恢复候选条款。"""
        raw_filter = data.get("rule_filter") or {}
        return cls(
            candidate_id=str(data["candidate_id"]),
            contract_name=str(data["contract_name"]),
            source_path=str(data["source_path"]),
            source_index=int(data.get("source_index", 0)),
            clause_id=str(data.get("clause_id", "")),
            clause_no=str(data.get("clause_no", "")),
            title=str(data.get("title", "")),
            section_path=str(data.get("section_path", "")),
            text=str(data.get("text", "")),
            rule_filter=RuleFilterResult(
                passed=bool(raw_filter.get("passed", False)),
                reasons=list(raw_filter.get("reasons") or []),
                score=int(raw_filter.get("score", 0)),
                dimensions=list(raw_filter.get("dimensions") or []),
                placeholder_ratio=float(raw_filter.get("placeholder_ratio", 0.0)),
            ),
            llm_filter=dict(data.get("llm_filter") or {}),
        )


@dataclass(slots=True)
class EvalSample:
    """单条款风险识别评测样本。"""

    sample_id: str
    gold_label: EvalLabel
    text: str
    contract_name: str
    source_path: str
    seed_candidate_id: str
    clause_no: str = ""
    title: str = ""
    section_path: str = ""
    variant_type: str = "safe"
    risk_pattern: str = ""
    expected_issue: str = ""
    generation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为可 JSON 序列化的字典。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvalSample":
        """从 JSON 字典恢复评测样本。"""
        return cls(
            sample_id=str(data["sample_id"]),
            gold_label=data["gold_label"],
            text=str(data["text"]),
            contract_name=str(data["contract_name"]),
            source_path=str(data["source_path"]),
            seed_candidate_id=str(data["seed_candidate_id"]),
            clause_no=str(data.get("clause_no", "")),
            title=str(data.get("title", "")),
            section_path=str(data.get("section_path", "")),
            variant_type=str(data.get("variant_type", "safe")),
            risk_pattern=str(data.get("risk_pattern", "")),
            expected_issue=str(data.get("expected_issue", "")),
            generation=dict(data.get("generation") or {}),
        )


@dataclass(slots=True)
class PredictionRecord:
    """评测运行后单个样本的预测记录。"""

    sample_id: str
    gold_label: EvalLabel
    predicted_label: EvalLabel | None
    predicted_system_level: str = ""
    success: bool = True
    error: str = ""
    elapsed_seconds: float = 0.0
    has_opinion: bool = False
    opinion_count: int = 0
    verified_citation_count: int = 0
    citation_count: int = 0
    raw_review: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为可 JSON 序列化的字典。"""
        return asdict(self)


def map_system_label(system_level: str) -> EvalLabel:
    """把系统五档风险等级映射成评测三档标签。"""
    normalized = (system_level or "").strip().lower()
    if normalized not in SYSTEM_TO_EVAL_LABEL:
        raise ValueError(f"未知系统风险等级：{system_level!r}")
    return SYSTEM_TO_EVAL_LABEL[normalized]
