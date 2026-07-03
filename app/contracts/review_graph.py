"""合同审查工作流（LangGraph StateGraph）。

把原来藏在 asyncio 原语里的隐式编排，改写成一张显式的状态图：

    START → parse_contract → classify_clauses → dispatch
              ─(conditional: list[Send])→ review_clause(并行 fan-out)
              ─(全部跳过/空)→ aggregate
    review_clause → aggregate → consistency_review → assemble_report → END

设计要点：
- 两个现有 agent（review agent / supervisor）不动，作为子单元被节点调用。
- 并发用 `Send` fan-out + 调用方 `config={"max_concurrency": N}` 限流，替代 Semaphore。
- 细粒度流式用 `get_stream_writer()` 把每条款的 think/tool/done 事件推到 custom 流，
  替代 asyncio.Queue fan-in；运行时自动做 fan-in。
- 错误隔离：review_clause 在节点内 try/except 兜住一切异常，失败写 `failed_clauses`
  并发 `clause_done(failed=true)`，**绝不 raise**——因为 Send fan-out 的所有分支同处
  一个事务性 superstep，任一分支抛异常会让整步回滚、丢掉全部 findings。

入口是 `get_review_graph()`（编译后的图，lru_cache 单例）；流式驱动见 review_pipeline.py。
"""

from __future__ import annotations

import asyncio
import json
import logging
import operator
import time
from functools import lru_cache
from typing import Annotated, Any

from langchain_core.messages import HumanMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send
from typing_extensions import TypedDict

from app.agents.session_locks import get_session_lock
from app.agents.supervisor import get_supervisor_agent
from app.contracts.audit import audit_event
from app.contracts.consistency_agent import (
    ConsistencyReviewNotSubmittedError,
    areview_consistency_events,
)
from app.contracts.ingest import ContractIngestPipeline
from app.contracts.prompts.overview_instruction import build_overview_instruction
from app.contracts.review_agent import _chunk_text, _chunk_thinking, areview_clause_events
from app.contracts.store import (
    ClauseRecord,
    ClauseRiskAssessmentRecord,
    ConsistencyFactRecord,
    ConsistencyOpinionRecord,
    ConsistencyRiskAssessmentRecord,
    ContractStore,
    ReviewOpinionRecord,
)
from app.core.config import settings
from app.core.observability import build_run_config
from app.llm.factory import get_default_chat_llm
from app.sessions.store import SessionStore

logger = logging.getLogger(__name__)

# 单条款审查的空闲超时：最后一道兜底（正常不触发）。工具或上游 LLM 若长时间无任何
# astream_events 事件，及时断路并收尾当前条款，避免前端执行图永久转圈。
_CLAUSE_IDLE_TIMEOUT_S = 120.0

# 条款分类的固定枚举；分类失败或越界时统一落到「其他」。
CLAUSE_CATEGORIES = [
    "核心义务",
    "付款结算",
    "违约责任",
    "争议解决",
    "知识产权",
    "保密",
    "信息与数据",
    "样板条款",
    "其他",
]
_BOILERPLATE_CATEGORY = "样板条款"
_FALLBACK_CATEGORY = "其他"

# 条款类别 → 重点审查维度（指引第6条六维）的静态映射。
# 用于聚焦每条款的审查注意力；缺省落到「内容合法性」。
CLAUSE_CATEGORY_TO_DIMENSIONS: dict[str, list[str]] = {
    "核心义务": ["权益明确性", "条款实用性"],
    "付款结算": ["权益明确性", "合同严谨性"],
    "违约责任": ["权益明确性", "合同严谨性"],
    "争议解决": ["条款实用性", "权益明确性"],
    "知识产权": ["内容合法性", "权益明确性"],
    "保密": ["权益明确性", "合同严谨性"],
    "信息与数据": ["内容合法性", "权益明确性"],
    "样板条款": ["表述精确性"],
    "其他": ["内容合法性"],
}


def focus_dimensions_for(category: str) -> list[str]:
    """按条款类别取重点审查维度；未知类别落到内容合法性。"""
    return CLAUSE_CATEGORY_TO_DIMENSIONS.get(category, ["内容合法性"])


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class ReviewState(TypedDict):
    contract_id: int
    contract_title: str
    session_id: str | None
    party_stance: str                               # 委托人立场（甲方/乙方/中立/未知）
    contract_clauses: list[ClauseRecord]            # 只读；parse_contract 填充
    clause_categories: dict[str, str]               # classify 产出 {clause_id: 类别}
    findings: Annotated[list[dict[str, Any]], operator.add]        # 并行 reducer：成功条款序列化意见
    failed_clauses: Annotated[list[dict[str, Any]], operator.add]  # 并行 reducer：{clause_id, reason}
    consistency_review: dict[str, Any]
    risk_count: int
    final_report: dict[str, Any]


class ClauseTask(TypedDict):
    """每个 Send 的载荷（review_clause 节点的输入）。"""

    contract_id: int
    contract_title: str
    clause: ClauseRecord
    category: str
    party_stance: str
    focus_dimensions: list[str]


# ---------------------------------------------------------------------------
# 推理步骤 / 风险序列化 helper（原 review_pipeline.py 迁移而来）
# ---------------------------------------------------------------------------

def _append_clause_thinking(steps: list[dict[str, Any]], delta: str) -> None:
    """把条款审查的 reasoning_content 增量合并到推理步骤。"""
    if not delta:
        return
    last = steps[-1] if steps else None
    if last and last.get("kind") == "thinking":
        last["text"] = f"{last.get('text', '')}{delta}"
        return
    steps.append({"kind": "thinking", "text": delta, "agent": "review_agent"})


def _push_clause_tool_start(
    steps: list[dict[str, Any]], *, call_id: str, name: str, args: dict[str, Any]
) -> None:
    steps.append(
        {
            "kind": "tool",
            "call": {
                "call_id": call_id,
                "name": name,
                "args": args,
                "status": "running",
                "agent": "review_agent",
                "startedAt": int(time.time() * 1000),
            },
        }
    )


def _patch_clause_tool_end(
    steps: list[dict[str, Any]],
    *,
    call_id: str,
    result_preview: str | None,
    citations: list[dict[str, Any]] | None,
) -> None:
    ended_at = int(time.time() * 1000)
    for step in reversed(steps):
        if step.get("kind") != "tool":
            continue
        call = step.get("call") or {}
        if call.get("call_id") != call_id:
            continue
        started_at = int(call.get("startedAt") or ended_at)
        call.update(
            {
                "status": "done",
                "result_preview": result_preview,
                "citations": citations or call.get("citations"),
                "endedAt": ended_at,
                "elapsed_ms": max(0, ended_at - started_at),
            }
        )
        return


def _finish_open_clause_tools(
    steps: list[dict[str, Any]], *, timed_out: bool
) -> list[dict[str, Any]]:
    """收尾只有 tool_start、没有 tool_end 的工具步骤，避免 UI 与落库 reasoning 永久 running。"""
    ended_at = int(time.time() * 1000)
    preview = "（审查超时，该工具调用未完成）" if timed_out else "（该工具调用未完成）"
    closed: list[dict[str, Any]] = []
    for step in steps:
        if step.get("kind") != "tool":
            continue
        call = step.get("call") or {}
        if call.get("status") != "running":
            continue
        started_at = int(call.get("startedAt") or ended_at)
        call.update(
            {
                "status": "done",
                "result_preview": preview,
                "citations": call.get("citations") or [],
                "endedAt": ended_at,
                "elapsed_ms": max(0, ended_at - started_at),
            }
        )
        closed.append(
            {
                "call_id": str(call.get("call_id") or ""),
                "name": str(call.get("name") or "tool"),
                "result_preview": preview,
                "citations": [],
            }
        )
    return closed


def _citation_to_dict(cit: Any) -> dict[str, Any]:
    return {
        "law_name": cit.law_name,
        "article_no": cit.article_no,
        "citation_text": cit.citation_text,
        "chunk_id": cit.chunk_id,
        "excerpt": cit.excerpt,
        "verified": cit.verified,
    }


def _opinion_to_dict(rec: ReviewOpinionRecord, clause: ClauseRecord | None = None) -> dict[str, Any]:
    return {
        "id": rec.id,
        "clause_id": clause.clause_id if clause else "",
        "clause_id_ref": rec.clause_id_ref,
        "opinion_type": rec.opinion_type,
        "review_dimension": rec.review_dimension,
        "finding": rec.finding,
        "recommendation": rec.recommendation,
        "confidence": rec.confidence,
        "citations": [_citation_to_dict(c) for c in rec.citations],
    }


def _assessment_to_dict(rec: ClauseRiskAssessmentRecord | None, clause: ClauseRecord | None = None) -> dict[str, Any] | None:
    if rec is None:
        return None
    data = rec.to_dict()
    if clause:
        data["clause_id"] = clause.clause_id
    return data


def _fact_to_dict(rec: ConsistencyFactRecord, clause: ClauseRecord | None = None) -> dict[str, Any]:
    data = rec.to_dict()
    if clause:
        data["clause_id"] = clause.clause_id
    return data


def _consistency_opinion_to_dict(rec: ConsistencyOpinionRecord) -> dict[str, Any]:
    return rec.to_dict()


def _consistency_risk_to_dict(rec: ConsistencyRiskAssessmentRecord | None) -> dict[str, Any] | None:
    return rec.to_dict() if rec else None


def _persist_clause_review(contract_id: int, clause: ClauseRecord, review: Any) -> dict[str, Any]:
    """把单条款审查结果写入新意见/风险评估/一致性事实表（同步）。"""
    if review is None:
        return {"opinions": [], "risk_assessment": None, "consistency_facts": []}
    out: list[dict[str, Any]] = []
    for opinion in review.opinions:
        try:
            rec = ContractStore.insert_review_opinion(
                contract_id=contract_id,
                clause_db_id=clause.id,
                opinion_type=opinion.opinion_type,
                review_dimension=opinion.review_dimension,
                finding=opinion.finding,
                recommendation=opinion.recommendation,
                confidence=opinion.confidence,
                citations=[c.model_dump() for c in opinion.citations],
            )
            out.append(_opinion_to_dict(rec, clause))
        except Exception:
            logger.exception("写入 review_opinion 失败 contract=%s clause=%s", contract_id, clause.clause_id)

    risk_rec = None
    try:
        ra = review.risk_assessment
        risk_rec = ContractStore.upsert_clause_risk_assessment(
            contract_id=contract_id,
            clause_db_id=clause.id,
            risk_level=ra.risk_level,
            rationale=ra.rationale,
            affected_party=ra.affected_party,
            confidence=ra.confidence,
        )
    except Exception:
        logger.exception("写入 clause_risk_assessment 失败 contract=%s clause=%s", contract_id, clause.clause_id)

    facts_payload: list[dict[str, Any]] = []
    for fact in review.consistency_facts:
        try:
            rec = ContractStore.insert_consistency_fact(
                contract_id=contract_id,
                clause_db_id=clause.id,
                category=fact.category,
                fact_key=fact.key,
                party=fact.party,
                value_text=fact.value_text,
                normalized_value=fact.normalized_value,
                span_text=fact.span_text,
                related_text=fact.related_text,
                confidence=fact.confidence,
            )
            facts_payload.append(_fact_to_dict(rec, clause))
        except Exception:
            logger.exception("写入 consistency_fact 失败 contract=%s clause=%s", contract_id, clause.clause_id)

    audit_event(
        "clause_review.submitted",
        contract_id=contract_id,
        clause_id=clause.clause_id,
        clause_no=clause.clause_no,
        node="review_clause",
        agent="review_agent",
        status="ok",
        has_opinion=bool(review.has_opinion),
        opinion_count=len(out),
        risk_assessment=_assessment_to_dict(risk_rec, clause),
        consistency_fact_count=len(facts_payload),
    )
    return {
        "opinions": out,
        "risk_assessment": _assessment_to_dict(risk_rec, clause),
        "consistency_facts": facts_payload,
    }


# ---------------------------------------------------------------------------
# 条款分类
# ---------------------------------------------------------------------------

def _build_classify_prompt(clauses: list[ClauseRecord]) -> str:
    lines = [
        f"{i + 1}. clause_id={c.clause_id}｜编号={c.clause_no or '无'}｜标题={c.title or '无'}"
        f"｜正文={c.text.strip()[:80].replace(chr(10), ' ')}"
        for i, c in enumerate(clauses)
    ]
    catalog = "、".join(CLAUSE_CATEGORIES)
    return (
        "你是合同条款分类助手。请把下面每条款归入且仅归入一个类别。\n"
        f"可选类别（必须严格使用其一）：{catalog}。\n"
        "「样板条款」指标题/定义/送达地址/签署页等无实质权利义务的格式条款。\n\n"
        "条款列表：\n" + "\n".join(lines) + "\n\n"
        "只输出一个 JSON 对象，键是 clause_id，值是类别字符串，"
        "不要输出任何额外文字或代码块标记。例如：{\"c1\": \"核心义务\", \"c2\": \"样板条款\"}"
    )


def _parse_category_json(text: str, clauses: list[ClauseRecord]) -> dict[str, str]:
    """从 LLM 输出里宽松解析 {clause_id: category}，越界/缺失统一落到「其他」。"""
    valid_ids = {c.clause_id for c in clauses}
    mapping: dict[str, str] = {c.clause_id: _FALLBACK_CATEGORY for c in clauses}
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return mapping
    try:
        parsed = json.loads(text[start : end + 1])
    except (TypeError, ValueError, json.JSONDecodeError):
        return mapping
    if not isinstance(parsed, dict):
        return mapping
    for key, value in parsed.items():
        cid = str(key)
        cat = str(value).strip()
        if cid in valid_ids and cat in CLAUSE_CATEGORIES:
            mapping[cid] = cat
    return mapping


async def _classify(clauses: list[ClauseRecord], contract_id: int) -> dict[str, str]:
    if not clauses:
        return {}
    try:
        llm = get_default_chat_llm()
        resp = await llm.ainvoke(
            [HumanMessage(content=_build_classify_prompt(clauses))],
            config=build_run_config(
                run_name="classify_clauses",
                tags=["classify"],
                metadata={"contract_id": contract_id, "clause_count": len(clauses)},
            ),
        )
        content = getattr(resp, "content", "")
        text = content if isinstance(content, str) else _chunk_text(content)
        return _parse_category_json(text, clauses)
    except Exception:
        logger.exception("条款分类失败，降级为全部按需审查 contract=%s", contract_id)
        return {c.clause_id: _FALLBACK_CATEGORY for c in clauses}


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------

async def parse_contract(state: ReviewState) -> dict[str, Any]:
    """解析/切分/写 PG+Milvus（复用 ContractIngestPipeline），产出条款列表。"""
    writer = get_stream_writer()
    contract_id = state["contract_id"]
    writer({"event": "status", "data": {"status": "parsing"}})
    # 审查真正开始：落 started_at（审查起始时刻），既驱动前端时间线排序，
    # 也持久化 parsing 状态，便于解析阶段断线重连时识别为「进行中」。
    await asyncio.to_thread(
        ContractStore.update_status, contract_id, status="parsing", start=True
    )
    started = time.perf_counter()
    logger.info("解析合同开始 contract=%s", contract_id)
    ingest = await asyncio.to_thread(ContractIngestPipeline().run, contract_id)
    contract = await asyncio.to_thread(ContractStore.get_by_id, contract_id)
    title = ingest.parsed_doc.title or (contract.filename if contract else state["contract_title"])
    logger.info(
        "解析合同完成 contract=%s clauses=%s elapsed=%.0fms",
        contract_id, len(ingest.clause_records), (time.perf_counter() - started) * 1000,
    )
    return {
        "contract_clauses": ingest.clause_records,
        "contract_title": title,
        "party_stance": (contract.party_stance if contract else "未知"),
    }


async def classify_clauses(state: ReviewState) -> dict[str, Any]:
    """批量给条款打类别，并把带类别的条款目录发给前端预渲染。"""
    writer = get_stream_writer()
    clauses = state["contract_clauses"]
    categories = await _classify(clauses, state["contract_id"])
    logger.info(
        "条款分类完成 contract=%s total=%s", state["contract_id"], len(clauses)
    )
    await asyncio.to_thread(ContractStore.update_status, state["contract_id"], status="reviewing")
    writer(
        {
            "event": "status",
            "data": {
                "status": "reviewing",
                "total": len(clauses),
                "clauses": [
                    {
                        "clause_id": c.clause_id,
                        "clause_no": c.clause_no,
                        "title": c.title,
                        "category": categories.get(c.clause_id, _FALLBACK_CATEGORY),
                    }
                    for c in clauses
                ],
            },
        }
    )
    return {"clause_categories": categories}


async def dispatch(state: ReviewState) -> dict[str, Any]:
    """fan-out 决策点：开启 skip 时先为样板条款发 clause_done(skipped) 并落库。"""
    if not settings.review_skip_boilerplate:
        return {}
    writer = get_stream_writer()
    categories = state["clause_categories"]
    for clause in state["contract_clauses"]:
        if categories.get(clause.clause_id) != _BOILERPLATE_CATEGORY:
            continue
        # 错误隔离：单条样板条款落库失败只记日志、不中断 dispatch——否则会在 fan-out 前
        # 整图失败，丢掉全部条款审查。与 review_clause 的错误隔离原则保持一致。
        try:
            await asyncio.to_thread(
                ContractStore.update_clause_review,
                clause.id,
                review_status="skipped",
                review_has_risk=False,
                reasoning=[],
            )
        except Exception:
            logger.exception(
                "标记样板条款 skipped 失败 contract=%s clause=%s",
                state["contract_id"], clause.clause_id,
            )
        writer(
            {
                "event": "clause_done",
                "data": {
                    "clause_id": clause.clause_id,
                    "has_opinion": False,
                    "opinions": [],
                    "risk_assessment": None,
                    "skipped": True,
                },
            }
        )
    return {}


def fan_out(state: ReviewState):
    """条件边：为每条「需审查」条款 Send 到 review_clause；无可审查条款时直达 aggregate。"""
    categories = state["clause_categories"]
    skip = settings.review_skip_boilerplate
    party_stance = state.get("party_stance", "未知")
    sends: list[Send] = []
    for clause in state["contract_clauses"]:
        category = categories.get(clause.clause_id, _FALLBACK_CATEGORY)
        if skip and category == _BOILERPLATE_CATEGORY:
            continue
        sends.append(
            Send(
                "review_clause",
                {
                    "contract_id": state["contract_id"],
                    "contract_title": state["contract_title"],
                    "clause": clause,
                    "category": category,
                    "party_stance": party_stance,
                    "focus_dimensions": focus_dimensions_for(category),
                },
            )
        )
    return sends or "aggregate"


async def review_clause(task: ClauseTask) -> dict[str, Any]:
    """审查单条款（调用现有 review agent），流式推送推理事件；错误全兜在节点内。"""
    writer = get_stream_writer()
    clause = task["clause"]
    contract_id = task["contract_id"]
    cid = clause.clause_id
    try:
        reasoning_steps: list[dict[str, Any]] = []
        await asyncio.to_thread(
            ContractStore.update_clause_review,
            clause.id,
            review_status="reviewing",
            review_has_risk=False,
            reasoning=[],
        )
        writer(
            {
                "event": "clause_start",
                "data": {
                    "clause_id": cid,
                    "clause_no": clause.clause_no,
                    "title": clause.title,
                    "category": task.get("category", ""),
                },
            }
        )

        logger.debug("条款审查开始 contract=%s clause=%s", contract_id, cid)
        review = None
        timed_out = False
        agen = areview_clause_events(
            contract_title=task["contract_title"],
            section_path=clause.section_path,
            clause_no=clause.clause_no,
            clause_text=clause.text,
            party_stance=task.get("party_stance", "未知"),
            focus_dimensions=task.get("focus_dimensions") or None,
            # 嵌套在审查图节点内：不设 run_id（自动挂到审查图 root run），仅打 clause 级标识便于过滤。
            run_config=build_run_config(
                run_name=f"clause:{cid}",
                tags=["clause_review", "review_agent"],
                metadata={
                    "contract_id": contract_id,
                    "clause_id": cid,
                    "category": task.get("category", ""),
                },
            ),
        )
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(agen.__anext__(), _CLAUSE_IDLE_TIMEOUT_S)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    timed_out = True
                    logger.warning(
                        "条款 %s 审查空闲超时（%.0fs 无事件），跳过",
                        cid, _CLAUSE_IDLE_TIMEOUT_S,
                    )
                    break
                t = ev["type"]
                if t == "think":
                    _append_clause_thinking(reasoning_steps, ev["delta"])
                    writer({"event": "clause_think_delta", "data": {
                        "clause_id": cid, "delta": ev["delta"], "agent": "review_agent"}})
                elif t == "tool_start":
                    _push_clause_tool_start(
                        reasoning_steps, call_id=ev["call_id"], name=ev["name"], args=ev["args"]
                    )
                    writer({"event": "clause_tool_start", "data": {
                        "clause_id": cid, "call_id": ev["call_id"],
                        "name": ev["name"], "args": ev["args"], "agent": "review_agent"}})
                elif t == "tool_end":
                    _patch_clause_tool_end(
                        reasoning_steps, call_id=ev["call_id"],
                        result_preview=ev["result_preview"], citations=ev["citations"],
                    )
                    writer({"event": "clause_tool_end", "data": {
                        "clause_id": cid, "call_id": ev["call_id"], "name": ev["name"],
                        "result_preview": ev["result_preview"],
                        "citations": ev["citations"], "agent": "review_agent"}})
                elif t == "result":
                    review = ev["review"]
        finally:
            await agen.aclose()

        for closed_tool in _finish_open_clause_tools(reasoning_steps, timed_out=timed_out):
            writer({"event": "clause_tool_end", "data": {
                "clause_id": cid,
                "call_id": closed_tool["call_id"],
                "name": closed_tool["name"],
                "result_preview": closed_tool["result_preview"],
                "citations": closed_tool["citations"],
                "agent": "review_agent",
            }})

        persisted = await asyncio.to_thread(
            _persist_clause_review, contract_id, clause, review
        )
        opinions_payload = persisted["opinions"]
        risk_assessment_payload = persisted["risk_assessment"]
        has_opinion = bool(review and review.has_opinion)
        await asyncio.to_thread(
            ContractStore.update_clause_review,
            clause.id,
            review_status="done",
            review_has_risk=bool(
                risk_assessment_payload
                and risk_assessment_payload.get("risk_level") != "none"
            ),
            reasoning=reasoning_steps,
        )
        logger.debug(
            "条款审查完成 contract=%s clause=%s opinions=%s timed_out=%s",
            contract_id, cid, len(opinions_payload), timed_out,
        )
        writer({"event": "clause_done", "data": {
            "clause_id": cid,
            "has_opinion": has_opinion,
            "opinions": opinions_payload,
            "risk_assessment": risk_assessment_payload,
            "timed_out": timed_out,
        }})
        return {"findings": opinions_payload}

    except Exception as exc:
        # 错误隔离：绝不向上抛，否则会让整个并行 superstep 事务回滚、丢掉其它条款结果。
        logger.exception("条款审查失败 contract=%s clause=%s", contract_id, cid)
        try:
            await asyncio.to_thread(
                ContractStore.update_clause_review,
                clause.id,
                review_status="failed",
                review_has_risk=False,
                reasoning=[],
            )
        except Exception:
            logger.exception("标记条款失败状态失败 clause=%s", cid)
        try:
            writer({"event": "clause_done", "data": {
                "clause_id": cid,
                "has_opinion": False,
                "opinions": [],
                "risk_assessment": None,
                "failed": True,
            }})
        except Exception:
            pass
        return {"failed_clauses": [{"clause_id": cid, "reason": f"{type(exc).__name__}: {exc}"}]}


async def aggregate(state: ReviewState) -> dict[str, Any]:
    """fan-in：统计条款级风险评估与意见数量，汇总失败条款。"""
    contract_id = state["contract_id"]
    all_opinions = await asyncio.to_thread(ContractStore.list_review_opinions, contract_id)
    assessments = await asyncio.to_thread(ContractStore.list_clause_risk_assessments, contract_id)
    risk_total = sum(1 for item in assessments if item.risk_level != "none")
    by_level = {level: 0 for level in ("critical", "high", "medium", "low", "none")}
    for item in assessments:
        by_level[item.risk_level] = by_level.get(item.risk_level, 0) + 1
    failed = state.get("failed_clauses", [])
    # 条款级审查结果此刻已全部落库并完成 fan-in：先发 report_ready，让前端预取条款意见。
    # 报告气泡仍需等一致性审查结束后再展示，避免用户看到缺少全文一致性结论的半成品报告。
    writer = get_stream_writer()
    writer({"event": "report_ready", "data": {
        "risk_count": risk_total,
        "opinion_count": len(all_opinions),
    }})
    return {
        "risk_count": risk_total,
        "final_report": {
            "risk_count": risk_total,
            "opinion_count": len(all_opinions),
            "risk_distribution": by_level,
            "failed_clauses": failed,
        },
    }


def _build_consistency_payload(contract_id: int, clauses: list[ClauseRecord]) -> dict[str, Any]:
    clauses_by_db_id = {c.id: c for c in clauses}
    opinions = ContractStore.list_review_opinions(contract_id)
    assessments = ContractStore.list_clause_risk_assessments(contract_id)
    facts = ContractStore.list_consistency_facts(contract_id)
    return {
        "clauses": [
            {
                "id": c.id,
                "clause_id": c.clause_id,
                "clause_no": c.clause_no,
                "title": c.title,
                "text_preview": c.text[:500],
            }
            for c in clauses
        ],
        "opinions": [
            _opinion_to_dict(opinion, clauses_by_db_id.get(opinion.clause_id_ref))
            for opinion in opinions
        ],
        "clause_risk_assessments": [
            _assessment_to_dict(assessment, clauses_by_db_id.get(assessment.clause_id_ref))
            for assessment in assessments
        ],
        "consistency_facts": [
            _fact_to_dict(fact, clauses_by_db_id.get(fact.clause_id_ref))
            for fact in facts
        ],
    }


def _persist_consistency_review(contract_id: int, review: Any) -> dict[str, Any]:
    opinions_payload: list[dict[str, Any]] = []
    for opinion in review.opinions:
        rec = ContractStore.insert_consistency_opinion(
            contract_id=contract_id,
            opinion_type=opinion.opinion_type,
            review_dimension=opinion.review_dimension,
            finding=opinion.finding,
            recommendation=opinion.recommendation,
            related_clause_ids=opinion.related_clause_ids,
            evidence_facts=opinion.evidence_facts,
            confidence=opinion.confidence,
        )
        opinions_payload.append(_consistency_opinion_to_dict(rec))

    risk = review.risk_assessment
    risk_rec = ContractStore.upsert_consistency_risk_assessment(
        contract_id=contract_id,
        risk_level=risk.risk_level,
        rationale=risk.rationale,
        affected_party=risk.affected_party,
        confidence=risk.confidence,
    )
    return {
        "has_opinion": bool(review.has_opinion),
        "opinions": opinions_payload,
        "risk_assessment": _consistency_risk_to_dict(risk_rec),
        "note": review.note,
    }


async def consistency_review(state: ReviewState) -> dict[str, Any]:
    """合同级一致性审查：基于所有条款 agent 提交结果做横向比对。"""
    writer = get_stream_writer()
    contract_id = state["contract_id"]
    session_id = state.get("session_id")
    started = time.perf_counter()
    writer({"event": "consistency_start", "data": {"contract_id": contract_id}})
    audit_event(
        "consistency_review.started",
        contract_id=contract_id,
        session_id=session_id,
        node="consistency_review",
        agent="consistency_agent",
        status="started",
    )
    try:
        payload = await asyncio.to_thread(
            _build_consistency_payload,
            contract_id,
            state["contract_clauses"],
        )
        writer({"event": "consistency_delta", "data": {
            "message": "正在进行合同一致性审查",
            "fact_count": len(payload.get("consistency_facts") or []),
        }})
        # 流式消费一致性 agent 的推理：把 think 增量逐条推给前端（与条款级审查同样的「思考过程」展示）。
        review = None
        agen = areview_consistency_events(
            payload,
            run_config=build_run_config(
                run_name=f"consistency:{contract_id}",
                tags=["consistency_review", "consistency_agent"],
                metadata={
                    "contract_id": contract_id,
                    "session_id": session_id,
                    "fact_count": len(payload.get("consistency_facts") or []),
                },
            ),
        )
        try:
            async for ev in agen:
                t = ev["type"]
                if t == "think":
                    writer({"event": "consistency_think_delta", "data": {
                        "delta": ev["delta"], "agent": "consistency_agent"}})
                elif t == "result":
                    review = ev["review"]
        finally:
            await agen.aclose()
        if review is None:
            raise ConsistencyReviewNotSubmittedError("一致性审查流未产出结果")
        persisted = await asyncio.to_thread(_persist_consistency_review, contract_id, review)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        audit_event(
            "consistency_review.submitted",
            contract_id=contract_id,
            session_id=session_id,
            node="consistency_review",
            agent="consistency_agent",
            duration_ms=elapsed_ms,
            status="ok",
            opinion_count=len(persisted["opinions"]),
            risk_assessment=persisted["risk_assessment"],
        )
        writer({"event": "consistency_done", "data": persisted})
        return {"consistency_review": persisted}
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.exception("一致性审查失败 contract=%s", contract_id)
        audit_event(
            "consistency_review.failed",
            contract_id=contract_id,
            session_id=session_id,
            node="consistency_review",
            agent="consistency_agent",
            duration_ms=elapsed_ms,
            status="failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        writer({"event": "consistency_done", "data": {
            "failed": True,
            "error": f"{type(exc).__name__}: {exc}",
        }})
        return {"consistency_review": {"failed": True, "error": str(exc)}}


async def _generate_overview(state: ReviewState) -> None:
    """收尾：在 chat 线程上经顶层 supervisor 图生成总览，流式推送并落库。

    总览以 ``thread_id == session_id`` 运行，写入同一会话的 checkpointer 线程。这样后续追问
    能直接继承总览指令、工具结论和总览正文；同时用会话锁避免后台总览和用户追问并发写同一线程。
    """
    writer = get_stream_writer()
    contract_id = state["contract_id"]
    session_id = state.get("session_id")

    if not session_id:
        logger.warning("生成总览跳过：contract=%s 缺少 session_id", contract_id)
        writer({"event": "overview_done", "data": {}})
        return

    try:
        agent = get_supervisor_agent()
        writer({"event": "overview_start", "data": {"contract_id": contract_id}})
        audit_event(
            "overview.started",
            contract_id=contract_id,
            session_id=session_id,
            node="assemble_report",
            agent="supervisor",
            status="started",
            consistency_review=state.get("consistency_review") or {},
        )
        started = time.perf_counter()
        answer_parts: list[str] = []
        thinking_parts: list[str] = []
        config = build_run_config(
            run_name=f"overview:{contract_id}",
            tags=["overview", "supervisor"],
            metadata={"contract_id": contract_id, "session_id": session_id},
        )
        config["configurable"] = {"thread_id": session_id}
        async with get_session_lock(session_id):
            async for ev in agent.astream_events(
                {
                    "messages": [
                        HumanMessage(
                            content=build_overview_instruction(
                                state.get("party_stance", "未知")
                            )
                        )
                    ],
                    "contract_id": contract_id,
                },
                version="v2",
                config=config,
                subgraphs=True,
            ):
                if ev.get("event") != "on_chat_model_stream":
                    continue
                chunk = (ev.get("data") or {}).get("chunk")
                if chunk is None:
                    continue
                if getattr(chunk, "tool_call_chunks", None) or getattr(chunk, "tool_calls", None):
                    continue
                content = getattr(chunk, "content", "")
                think = _chunk_thinking(content) or (
                    getattr(chunk, "additional_kwargs", {}) or {}
                ).get("reasoning_content", "")
                if think:
                    thinking_parts.append(think)
                    writer({"event": "overview_think_delta", "data": {"delta": think, "agent": "supervisor"}})
                text = _chunk_text(content)
                if text:
                    answer_parts.append(text)
                    writer({"event": "overview_delta", "data": {"delta": text, "agent": "supervisor"}})

        answer = "".join(answer_parts).strip()
        writer({"event": "overview_done", "data": {}})
        audit_event(
            "overview.completed",
            contract_id=contract_id,
            session_id=session_id,
            node="assemble_report",
            agent="supervisor",
            status="ok",
            duration_ms=int((time.perf_counter() - started) * 1000),
            output_chars=len(answer),
        )
        if answer:
            await asyncio.to_thread(
                SessionStore.append_message,
                session_id,
                "assistant",
                answer,
                thinking=("".join(thinking_parts).strip() or None),
            )
    except Exception:
        logger.exception("生成合同总览失败 contract=%s", contract_id)
        audit_event(
            "overview.failed",
            contract_id=contract_id,
            session_id=session_id,
            node="assemble_report",
            agent="supervisor",
            status="failed",
        )
        try:
            writer({"event": "overview_done", "data": {}})
        except Exception:
            pass


async def assemble_report(state: ReviewState) -> dict[str, Any]:
    """生成总览 + 收尾状态机 + 发终结 done 事件。"""
    contract_id = state["contract_id"]
    risk_total = state.get("risk_count", 0)
    failed = (state.get("final_report") or {}).get("failed_clauses", [])

    await _generate_overview(state)

    await asyncio.to_thread(
        ContractStore.update_status,
        contract_id,
        status="done",
        risk_count=risk_total,
        finish=True,
    )
    logger.info(
        "合同审查完成 contract=%s clauses=%s risky_clauses=%s failed=%s",
        contract_id, len(state["contract_clauses"]), risk_total, len(failed),
    )
    writer = get_stream_writer()
    writer({"event": "done", "data": {
        "status": "done", "risk_count": risk_total, "failed_count": len(failed)}})
    return {}


# ---------------------------------------------------------------------------
# 图构建
# ---------------------------------------------------------------------------

def build_review_graph() -> CompiledStateGraph:
    g = StateGraph(ReviewState)
    g.add_node("parse_contract", parse_contract)
    g.add_node("classify_clauses", classify_clauses)
    g.add_node("dispatch", dispatch)
    g.add_node("review_clause", review_clause)
    g.add_node("aggregate", aggregate)
    g.add_node("review_consistency", consistency_review)
    g.add_node("assemble_report", assemble_report)

    g.add_edge(START, "parse_contract")
    g.add_edge("parse_contract", "classify_clauses")
    g.add_edge("classify_clauses", "dispatch")
    g.add_conditional_edges("dispatch", fan_out, ["review_clause", "aggregate"])
    g.add_edge("review_clause", "aggregate")
    g.add_edge("aggregate", "review_consistency")
    g.add_edge("review_consistency", "assemble_report")
    g.add_edge("assemble_report", END)
    return g.compile()


@lru_cache(maxsize=1)
def get_review_graph() -> CompiledStateGraph:
    """进程级单例编译图。"""
    return build_review_graph()


__all__ = ["ReviewState", "ClauseTask", "build_review_graph", "get_review_graph"]
