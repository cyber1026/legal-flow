"""合同条款规则筛选与评分。"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from app.contracts.clause_splitter import Clause

from eval.contract_clause_risk_review.markdown_loader import contract_id_from_path, split_standard_contract
from eval.contract_clause_risk_review.schemas import CandidateClause, RuleFilterResult

MEANINGFUL_DIMENSION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "付款价款": ("付款", "价款", "货款", "费用", "定金", "预付款", "结算", "支付", "退款"),
    "交付验收": ("交付", "交货", "提货", "验收", "检验", "收货", "交接", "过户", "登记"),
    "质量权利": ("质量", "瑕疵", "保修", "担保", "抵押", "租赁", "权属", "资质", "许可证"),
    "违约解除": ("违约", "赔偿", "损失", "解除", "终止", "逾期", "责任", "违约金"),
    "争议送达": ("争议", "仲裁", "法院", "管辖", "诉讼", "送达", "通知"),
    "信息合规": ("个人信息", "隐私", "保密", "数据", "披露"),
}

OBLIGATION_KEYWORDS = (
    "应",
    "须",
    "不得",
    "有权",
    "负责",
    "承担",
    "保证",
    "承诺",
    "支付",
    "交付",
    "赔偿",
    "解除",
)

SIGNATURE_KEYWORDS = (
    "签订地点",
    "签订时间",
    "出卖人（章）",
    "买受人（章）",
    "甲方（章）",
    "乙方（章）",
    "法定代表人",
    "委托代理人",
    "开户银行",
    "邮政编码",
    "监制部门",
    "印制单位",
)

PURE_INFO_TITLE_KEYWORDS = ("种类", "定义", "术语", "目录", "说明", "卡种")
PLACEHOLDER_RE = re.compile(r"[_＿]{2,}|□|（\s*）|\(\s*\)|年\s*月\s*日")


def _compact_text(text: str) -> str:
    """压缩空白后返回文本。"""
    return re.sub(r"\s+", "", text or "")


def placeholder_ratio(text: str) -> float:
    """估算条款中的空白占位符比例。"""
    compact = _compact_text(text)
    if not compact:
        return 1.0
    placeholder_chars = 0
    for match in PLACEHOLDER_RE.finditer(text):
        placeholder_chars += len(_compact_text(match.group(0)))
    underline_chars = compact.count("_") + compact.count("＿")
    box_chars = compact.count("□")
    placeholder_chars += underline_chars + box_chars
    return min(1.0, placeholder_chars / max(len(compact), 1))


def detect_dimensions(text: str) -> list[str]:
    """根据关键词识别条款可评测风险维度。"""
    dimensions: list[str] = []
    for dimension, keywords in MEANINGFUL_DIMENSION_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            dimensions.append(dimension)
    return dimensions


def _is_table_only(text: str) -> bool:
    """判断条款是否主要由 Markdown 表格行构成。"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    table_lines = [line for line in lines if line.startswith("|") and line.endswith("|")]
    return len(table_lines) / len(lines) >= 0.75


def _is_signature_or_meta(text: str) -> bool:
    """判断条款是否是签章页或元信息。"""
    compact = _compact_text(text)
    hits = sum(1 for keyword in SIGNATURE_KEYWORDS if keyword in compact)
    return hits >= 3


def _is_pure_definition(clause: Clause, text: str, dimensions: list[str]) -> bool:
    """判断是否是无交易风险承载的定义/分类/说明条款。"""
    title = clause.title or ""
    compact = _compact_text(text)
    if any(keyword in title for keyword in PURE_INFO_TITLE_KEYWORDS):
        if ("包括" in compact or "分为" in compact or "是指" in compact) and not dimensions:
            return True
    if ("包括" in compact and ("两大类" in compact or "类别" in compact)) and not dimensions:
        return True
    if ("是指" in compact or "以下简称" in compact) and not dimensions:
        obligation_hits = sum(1 for keyword in OBLIGATION_KEYWORDS if keyword in compact)
        return obligation_hits == 0
    return False


def score_clause(clause: Clause) -> tuple[int, list[str]]:
    """对条款进行规则评分并返回 `(分数, 维度列表)`。"""
    text = clause.text or ""
    dimensions = detect_dimensions(text)
    score = len(dimensions) * 3
    compact = _compact_text(text)
    score += sum(1 for keyword in OBLIGATION_KEYWORDS if keyword in compact)
    if len(compact) >= 180:
        score += 2
    elif len(compact) >= 100:
        score += 1
    return score, dimensions


def rule_filter_clause(clause: Clause) -> RuleFilterResult:
    """用规则判断条款是否适合作为评测 seed 候选。"""
    text = clause.text or ""
    compact = _compact_text(text)
    reasons: list[str] = []
    ratio = placeholder_ratio(text)
    score, dimensions = score_clause(clause)

    if len(compact) < 40 or (len(compact) < 80 and not dimensions):
        reasons.append("too_short")
    if ratio > 0.35:
        reasons.append("too_many_placeholders")
    if _is_table_only(text):
        reasons.append("table_only")
    if _is_signature_or_meta(text):
        reasons.append("signature_or_meta")
    if _is_pure_definition(clause, text, dimensions):
        reasons.append("pure_definition_or_category")
    if not dimensions and score < 2:
        reasons.append("no_meaningful_risk_dimension")

    return RuleFilterResult(
        passed=not reasons,
        reasons=reasons,
        score=score,
        dimensions=dimensions,
        placeholder_ratio=round(ratio, 4),
    )


def build_candidates_from_contract(path: Path, *, max_clause_chars: int = 1600) -> list[CandidateClause]:
    """从单份标准合同中抽取候选条款并附加规则筛选结果。"""
    contract_hash = contract_id_from_path(path)
    clauses = split_standard_contract(path, max_clause_chars=max_clause_chars)
    candidates: list[CandidateClause] = []
    for index, clause in enumerate(clauses):
        candidate_id = f"{contract_hash}-{index + 1:03d}"
        candidates.append(
            CandidateClause(
                candidate_id=candidate_id,
                contract_name=path.stem,
                source_path=str(path),
                source_index=index,
                clause_id=clause.clause_id,
                clause_no=clause.clause_no,
                title=clause.title,
                section_path=clause.section_path,
                text=clause.text,
                rule_filter=rule_filter_clause(clause),
            )
        )
    return candidates


def group_candidates_by_contract(candidates: list[CandidateClause]) -> dict[str, list[CandidateClause]]:
    """按合同名称聚合候选条款。"""
    grouped: dict[str, list[CandidateClause]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.contract_name].append(candidate)
    return dict(grouped)
