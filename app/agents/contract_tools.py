"""Contract-aware tools for the session supervisor.

包含两类工具：
- 只读工具：读取当前会话合同的条款目录、单条原文、结构化意见与风险评估。
- 路由工具：start_contract_review —— 用户在对话中明确要求"重新审查整份合同"时由 LLM 调起；
  本工具本身只返回一句中文确认（return_direct=True），实际触发审查后台任务的副作用放在
  supervisor 顶层图的 enqueue_review 节点里（见 app/agents/supervisor.py），与工具体解耦，
  便于错误隔离与可观察性。
"""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from app.contracts.store import (
    ClauseRecord,
    ClauseRiskAssessmentRecord,
    ConsistencyOpinionRecord,
    ConsistencyRiskAssessmentRecord,
    ContractRecord,
    ContractStore,
    ReviewOpinionRecord,
)

_LEVEL_LABELS = {
    "critical": "严重",
    "high": "高危",
    "medium": "中危",
    "low": "低危",
    "none": "无风险",
}


def _contract_id_from_state(state: Any) -> int | None:
    if isinstance(state, dict):
        raw = state.get("contract_id")
    else:
        raw = getattr(state, "contract_id", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _get_contract(state: Any) -> ContractRecord | None:
    contract_id = _contract_id_from_state(state)
    if contract_id is None:
        return None
    return ContractStore.get_by_id(contract_id)


def _find_clause(contract_id: int, clause_ref: str) -> ClauseRecord | None:
    ref = str(clause_ref or "").strip()
    if not ref:
        return None

    direct = ContractStore.get_clause(contract_id, ref)
    if direct:
        return direct

    for clause in ContractStore.list_clauses(contract_id):
        candidates = {
            str(clause.id),
            clause.clause_id,
            clause.clause_no,
            clause.title,
        }
        if ref in candidates:
            return clause
        if clause.clause_no and ref.replace("条", "") == clause.clause_no.replace("条", ""):
            return clause
    return None


def _format_clause_line(clause: ClauseRecord) -> str:
    label = clause.clause_no or "标题"
    title = f" {clause.title}" if clause.title else ""
    return f"- {label}{title}"


def _format_opinion(opinion: ReviewOpinionRecord, clause: ClauseRecord | None) -> str:
    clause_label = (
        (clause.clause_no or clause.clause_id or str(clause.id))
        if clause
        else f"db_id={opinion.clause_id_ref}"
    )
    lines = [
        f"- [{opinion.opinion_type}] 条款 {clause_label}"
        f"（{opinion.review_dimension}）：{opinion.finding}",
    ]
    if opinion.recommendation:
        lines.append(f"  处理建议：{opinion.recommendation}")
    for cit in opinion.citations:
        cite = cit.citation_text or f"《{cit.law_name}》{cit.article_no}"
        mark = "已核实" if cit.verified else "未核实"
        lines.append(f"  依据（{mark}）：{cite}")
    return "\n".join(lines)


def _format_clause_assessment(item: ClauseRiskAssessmentRecord, clause: ClauseRecord | None) -> str:
    clause_label = (
        (clause.clause_no or clause.clause_id or str(clause.id))
        if clause
        else f"db_id={item.clause_id_ref}"
    )
    level = _LEVEL_LABELS.get(item.risk_level, item.risk_level)
    return (
        f"- [{level}] 条款 {clause_label}；受影响方：{item.affected_party}；"
        f"理由：{item.rationale}"
    )


def _format_consistency_opinion(item: ConsistencyOpinionRecord) -> str:
    clauses = "、".join(item.related_clause_ids) if item.related_clause_ids else "未指明"
    return (
        f"- [{item.opinion_type}] 涉及条款：{clauses}（{item.review_dimension}）：{item.finding}\n"
        f"  处理建议：{item.recommendation}"
    )


def _format_consistency_risk(item: ConsistencyRiskAssessmentRecord) -> str:
    level = _LEVEL_LABELS.get(item.risk_level, item.risk_level)
    return f"一致性整体风险：{level}；受影响方：{item.affected_party}；理由：{item.rationale}"


@tool("list_clauses")
def list_clauses(state: Annotated[dict, InjectedState]) -> str:
    """列出当前会话合同的条款目录。不要让用户提供合同 id。"""
    contract = _get_contract(state)
    if not contract:
        return "当前会话没有关联合同。"
    clauses = ContractStore.list_clauses(contract.id)
    if not clauses:
        return f"《{contract.title or contract.filename}》尚未解析出条款，可能审查任务还未完成。"
    return "\n".join(
        [f"合同：《{contract.title or contract.filename}》；共 {len(clauses)} 条。"]
        + [_format_clause_line(c) for c in clauses]
    )


@tool("get_clause")
def get_clause(clause_no_or_id: str, state: Annotated[dict, InjectedState]) -> str:
    """读取当前会话合同的单个条款原文；参数可传条款编号、clause_id 或 db_id。"""
    contract = _get_contract(state)
    if not contract:
        return "当前会话没有关联合同。"
    clause = _find_clause(contract.id, clause_no_or_id)
    if not clause:
        return f"未找到条款：{clause_no_or_id}。可先调用 list_clauses 查看可用编号。"
    head = " / ".join(p for p in [clause.section_path, clause.clause_no, clause.title] if p)
    return (
        f"合同：《{contract.title or contract.filename}》\n"
        f"条款：{head or clause.clause_id}（id={clause.clause_id}, db_id={clause.id}）\n\n"
        f"{clause.text}"
    )


@tool("get_opinions")
def get_opinions(
    state: Annotated[dict, InjectedState],
    clause: str = "",
) -> str:
    """读取当前会话合同的结构化审查意见。可按条款编号过滤。"""
    contract = _get_contract(state)
    if not contract:
        return "当前会话没有关联合同。"

    opinions = ContractStore.list_review_opinions(contract.id)
    clauses = ContractStore.list_clauses(contract.id)
    clauses_by_id = {c.id: c for c in clauses}

    clause_ref = (clause or "").strip()
    if clause_ref:
        found = _find_clause(contract.id, clause_ref)
        if not found:
            return f"未找到条款：{clause_ref}。可先调用 list_clauses 查看可用编号。"
        opinions = [r for r in opinions if r.clause_id_ref == found.id]

    if not opinions:
        return f"未查询到{('条款 ' + clause_ref + ' 的') if clause_ref else ''}审查意见。"

    header = (
        f"合同：《{contract.title or contract.filename}》；"
        f"审查意见共 {len(opinions)} 条。"
    )
    body = [_format_opinion(r, clauses_by_id.get(r.clause_id_ref)) for r in opinions]
    return "\n\n".join([header, *body])


@tool("get_clause_risk_assessments")
def get_clause_risk_assessments(
    state: Annotated[dict, InjectedState],
    level: str = "",
) -> str:
    """读取条款级综合风险评估。可按 level(critical/high/medium/low/none) 过滤。"""
    contract = _get_contract(state)
    if not contract:
        return "当前会话没有关联合同。"
    assessments = ContractStore.list_clause_risk_assessments(contract.id)
    level_key = (level or "").strip().lower()
    if level_key in _LEVEL_LABELS:
        assessments = [item for item in assessments if item.risk_level == level_key]
    if not assessments:
        return "未查询到条款级风险评估。"
    clauses_by_id = {c.id: c for c in ContractStore.list_clauses(contract.id)}
    by_level = {k: 0 for k in _LEVEL_LABELS}
    for item in assessments:
        by_level[item.risk_level] = by_level.get(item.risk_level, 0) + 1
    header = (
        f"合同：《{contract.title or contract.filename}》；"
        f"条款风险统计：严重 {by_level.get('critical', 0)} 条，"
        f"高危 {by_level.get('high', 0)} 条，"
        f"中危 {by_level.get('medium', 0)} 条，"
        f"低危 {by_level.get('low', 0)} 条，"
        f"无风险 {by_level.get('none', 0)} 条。"
    )
    body = [_format_clause_assessment(item, clauses_by_id.get(item.clause_id_ref)) for item in assessments]
    return "\n".join([header, *body])


@tool("get_consistency_opinions")
def get_consistency_opinions(state: Annotated[dict, InjectedState]) -> str:
    """读取合同级一致性审查意见。"""
    contract = _get_contract(state)
    if not contract:
        return "当前会话没有关联合同。"
    opinions = ContractStore.list_consistency_opinions(contract.id)
    if not opinions:
        return "未查询到合同一致性审查意见。"
    body = [_format_consistency_opinion(item) for item in opinions]
    return "\n\n".join([f"合同：《{contract.title or contract.filename}》；一致性意见 {len(opinions)} 条。", *body])


@tool("get_consistency_risk_assessment")
def get_consistency_risk_assessment(state: Annotated[dict, InjectedState]) -> str:
    """读取合同一致性层面的整体风险评估。"""
    contract = _get_contract(state)
    if not contract:
        return "当前会话没有关联合同。"
    item = ContractStore.get_consistency_risk_assessment(contract.id)
    if not item:
        return "未查询到合同一致性风险评估。"
    return _format_consistency_risk(item)


@tool("start_contract_review", return_direct=True)
def start_contract_review(
    reason: str,
    state: Annotated[dict, InjectedState],
    party_stance: str = "未知",
) -> str:
    """用户明确要求对当前合同发起或重新发起整份的全量结构化审查时调用本工具。

    参数 reason：用一句中文写明用户的审查诉求（如「用户要求重新审一遍」），仅用于日志追踪，
    不需要由用户提供；contract_id 由系统注入，不要让用户提供 id。
    参数 party_stance：若用户在话语中**明确表明了自己的立场**（如「我是甲方」「站在乙方」），
      传 "甲方"/"乙方"/"中立"；否则传 "未知"，系统会在审查前追问。不要臆测立场。

    本工具体仅返回一句中文确认（return_direct=True，子图随之终止）；真正的后台任务启动与
    review_started 自定义事件由 supervisor 顶层图的 enqueue_review 节点负责，立场确认由
    ensure_stance 节点负责（见 supervisor.py）。
    不要把本工具用于「会话内复审单条款 / 阅读条款 / 查看已有风险」之类的轻量任务——那些用
    get_clause / get_opinions / get_clause_risk_assessments 即可，无需重新跑后台审查。
    """
    _ = (reason, party_stance)  # 路由信号 + 立场由图节点读取，工具体不处理
    contract_id = _contract_id_from_state(state)
    if contract_id is None:
        return "当前会话未挂载合同，无法发起审查。请先在左侧面板上传合同文件。"
    # 关键：本工具只是「路由信号」，此刻审查【尚未启动】——真正启动在 ensure_stance 确认立场之后
    # 的 enqueue_review 节点。返回串必须如实说明，否则模型会误以为审查已在后台运行（例如用户随后
    # 取消时会答「审查已启动无法中断」），用户也会困惑「还没选立场怎么就开始了」。
    return (
        "已收到整份审查请求。系统会先确认你的委托人立场（未确认时会弹出立场选择卡片），"
        "确认立场后才正式发起后台审查。此刻审查尚未开始。"
    )


__all__ = [
    "get_clause",
    "get_opinions",
    "get_clause_risk_assessments",
    "get_consistency_opinions",
    "get_consistency_risk_assessment",
    "list_clauses",
    "start_contract_review",
]
