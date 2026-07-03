"""合同条款审查 Agent。

设计要点：
- 单 Agent + ReAct + 显式 submit_review 工具提交最终结构化结果
- 复用 [`law_tools`](../agents/law_tools.py) 的 verify_law_article / search_law，与问答 Agent 共享核验能力
- 进程级 lru_cache 单例，避免反复构建 LangGraph 编译产物
- 新范式：LLM 先用自身知识判断该引哪条法，再用工具核验；后端按 (law_name, article_no)
  比对「已核实集合」，命中标 verified、回填 chunk_id，未命中保留并标未核实
"""

from __future__ import annotations

import logging
import json
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agents.law_tools import search_law, verify_law_article
from app.knowledge.summarize import make_kb_tools
from app.contracts.prompts.review_system import REVIEW_SYSTEM_PROMPT
from app.contracts.prompts.risk_schema import (
    AffectedParty,
    ClauseRiskAssessment,
    ConsistencyCategory,
    ConsistencyFact,
    ConsistencyParty,
    OpinionType,
    ReviewDimension,
    ReviewOutput,
    ReviewCitation,
    ReviewOpinion,
    RiskLevel,
)
from app.core.config import settings
from app.llm.factory import get_chat_llm
from app.retrieval.article_no import normalize_article_no

logger = logging.getLogger(__name__)


class ClauseReviewNotSubmittedError(RuntimeError):
    """review agent 跑完仍未提交结构化审查结果（含强制补提交也失败）。

    由 review_clause 节点的 try/except 捕获并把条款标为 failed（需人工复核），
    绝不再静默伪装成 risk_level="none"——遵循「错误不静默」的工程纪律。
    """


def _coerce_party_value(value: Any, *, allow_third_party: bool) -> Any:
    """把 LLM 偶发输出的合同角色词归一到 schema 允许的主体枚举。

    不能根据「卖方/买方」直接猜测甲方或乙方，因为不同示范合同里的甲乙身份不稳定；
    这类角色词保守落到「未知」，具体角色仍保留在 key/value_text/span_text 等事实字段里。
    """
    if not isinstance(value, str):
        return value
    normalized = value.strip()
    allowed = {"甲方", "乙方", "双方", "不适用", "未知"}
    if allow_third_party:
        allowed.add("第三方")
    if normalized in allowed:
        return normalized
    if normalized in {"双方当事人", "当事人双方", "买卖双方", "合同双方", "各方", "双方共同"}:
        return "双方"
    if allow_third_party and normalized in {"第三人", "第三者", "第三方主体"}:
        return "第三方"
    if normalized in {"无", "无关", "不涉及", "不适用主体"}:
        return "不适用"
    if normalized in {
        "买方",
        "卖方",
        "买受人",
        "出卖人",
        "供方",
        "需方",
        "采购方",
        "销售方",
        "收货方",
        "交货方",
        "付款方",
        "收款方",
        "委托方",
        "受托方",
        "承租方",
        "出租方",
        "承揽方",
        "定作方",
        "服务方",
        "客户",
        "供应商",
    }:
        return "未知"
    return "未知"


# ---------------------------------------------------------------------------
# DeepSeek strict 工具
# ---------------------------------------------------------------------------

class ReviewCitationSubmission(BaseModel):
    """submit_review 的引用参数；溯源字段由后端按 (law_name, article_no) 核验回填。

    extra="ignore"：模型偶尔会给引用多塞 paragraph/段落等字段；宽容丢弃而非让整条款审查失败。
    （strict 模式下 langchain 仍会强制 additionalProperties=false，API 侧约束不受影响。）
    """

    model_config = ConfigDict(extra="ignore")

    law_name: str = Field(description="你核验过的法律全称，如「中华人民共和国民法典」")
    article_no: str = Field(description="你核验过的条文号，如「第四百九十七条」")
    citation_text: str = Field(description="标准引用文本，如「《民法典》第497条」")
    excerpt: str = Field(description="原文摘录，截取最相关的一两句；无直接依据时填空字符串")


class ReviewOpinionSubmission(BaseModel):
    """submit_review 的单条审查意见参数；不包含风险等级。

    extra="ignore" + confidence 默认值：见 ReviewCitationSubmission 注释——宽容模型偶发的多余字段/漏填软指标，
    避免单点小瑕疵让整条款审查失败、被迫人工复核。
    """

    model_config = ConfigDict(extra="ignore")

    opinion_type: OpinionType = Field(description="意见类型，从枚举中选一个：疑问/说明/提醒/建议/警告")
    review_dimension: ReviewDimension = Field(
        description="审查维度，从枚举中选一个：主体合格性/内容合法性/条款实用性/权益明确性/合同严谨性/表述精确性"
    )
    finding: str = Field(description="审查发现：指出什么问题、疑问或说明事项，1-3 句")
    recommendation: str = Field(description="处理建议：如何修改、补充、核验或谈判，1-3 句")
    confidence: float = Field(ge=0.0, le=1.0, description="判断置信度 0~1")
    citations: list[ReviewCitationSubmission] = Field(
        description="支撑该意见的法律依据；无法找到直接法条时填空数组"
    )


class ClauseRiskAssessmentSubmission(BaseModel):
    """submit_review 的条款级综合风险评估参数。"""

    model_config = ConfigDict(extra="ignore")

    risk_level: RiskLevel = Field(description="综合整个条款后的风险等级：none/low/medium/high/critical")
    rationale: str = Field(description="风险评级理由；应综合整条款，而不是绑定单条意见")
    affected_party: AffectedParty = Field(description="主要受影响的一方：甲方/乙方/双方/不适用/未知")
    confidence: float = Field(ge=0.0, le=1.0, description="判断置信度 0~1")

    @field_validator("affected_party", mode="before")
    @classmethod
    def _normalize_affected_party(cls, value: Any) -> Any:
        """把模型偶发输出的买卖角色词归一到合法枚举。"""
        return _coerce_party_value(value, allow_third_party=False)


class ConsistencyFactSubmission(BaseModel):
    """submit_review 的一致性事实参数；供合同级一致性审查横向比对。"""

    model_config = ConfigDict(extra="ignore")

    category: ConsistencyCategory = Field(description="事实类型")
    key: str = Field(description="事实名称，如甲方名称、付款期限、违约责任触发条件")
    party: ConsistencyParty = Field(description="关联主体")
    value_text: str = Field(description="条款原文中的事实值")
    normalized_value: str = Field(description="规范化后的值，用于跨条款比较")
    span_text: str = Field(description="支撑该事实的最小原文片段")
    related_text: str = Field(description="条件、例外、触发场景等上下文；没有则填空字符串")
    confidence: float = Field(ge=0.0, le=1.0, description="事实抽取置信度 0~1")

    @field_validator("party", mode="before")
    @classmethod
    def _normalize_party(cls, value: Any) -> Any:
        """把模型偶发输出的买卖角色词归一到合法枚举。"""
        return _coerce_party_value(value, allow_third_party=True)


class ReviewSubmission(BaseModel):
    """submit_review 的完整审查结果参数。"""

    model_config = ConfigDict(extra="ignore")

    has_opinion: bool = Field(description="是否产出任何审查意见")
    opinions: list[ReviewOpinionSubmission] = Field(
        description="审查意见列表；has_opinion=false 时填空数组"
    )
    risk_assessment: ClauseRiskAssessmentSubmission = Field(
        description="条款级综合风险评估；不要绑定到单条意见"
    )
    consistency_facts: list[ConsistencyFactSubmission] = Field(
        description="本条款可用于全合同一致性审查的结构化事实；没有则填空数组"
    )
    note: str = Field(description="补充说明；无补充说明时填空字符串")


@tool(
    "submit_review",
    args_schema=ReviewSubmission,
    return_direct=True,
    response_format="content_and_artifact",
)
def submit_review(**kwargs: Any) -> tuple[str, dict[str, Any]]:
    """提交最终合同条款审查结果；这是唯一合法的最终输出方式。"""
    submission = ReviewSubmission.model_validate(kwargs)
    return "审查结果已提交", {"review_output": submission.model_dump()}


class DeepSeekStrictToolMiddleware(AgentMiddleware):
    """让 DeepSeek beta strict 校验所有工具参数，但不强制 tool_choice。

    同步与异步两个入口都要实现：审查走 astream_events（async），其它路径可能 invoke（sync）。
    """

    def wrap_model_call(self, request, handler):
        model_settings = {**request.model_settings, "strict": True}
        return handler(request.override(model_settings=model_settings))

    async def awrap_model_call(self, request, handler):
        model_settings = {**request.model_settings, "strict": True}
        return await handler(request.override(model_settings=model_settings))


# ---------------------------------------------------------------------------
# Agent 构建
# ---------------------------------------------------------------------------

def _get_review_model():
    # review agent 需要长 reasoning（大输入 + 法库工具往返），单 chunk 空闲容易超过
    # 默认 llm_request_timeout（chat 用），故统一用更长的 llm_review_timeout。
    review_timeout = settings.llm_review_timeout
    if settings.llm_provider.lower() == "deepseek":
        return get_chat_llm(
            enable_thinking=True,
            base_url=settings.deepseek_beta_base_url,
            timeout=review_timeout,
        )
    return get_chat_llm(timeout=review_timeout)


def build_review_agent() -> CompiledStateGraph:
    """构建合同审查 Agent。"""
    middleware = []
    if settings.llm_provider.lower() == "deepseek":
        middleware.append(DeepSeekStrictToolMiddleware())
    # 工具集 = 法库核验工具 + 5 个审查支撑库检索总结工具（按需调用）+ submit_review（终态提交）。
    return create_agent(
        model=_get_review_model(),
        tools=[verify_law_article, search_law, *make_kb_tools(), submit_review],
        system_prompt=REVIEW_SYSTEM_PROMPT,
        middleware=middleware,
    )


@lru_cache(maxsize=1)
def get_review_agent() -> CompiledStateGraph:
    """进程级单例 Agent。"""
    return build_review_agent()


# ---------------------------------------------------------------------------
# 工具结果校验
# ---------------------------------------------------------------------------

def _collect_verified_articles(messages: list) -> dict[tuple[str, str], dict[str, Any]]:
    """从执行轨迹里收集「已核实集合」。

    本次审查期间 verify_law_article / search_law 任一工具从法库真实返回过的条文，
    按归一化 (law_name, article_no) 建索引，作为引用是否经法库核实的判据。
    """
    verified: dict[tuple[str, str], dict[str, Any]] = {}
    for msg in messages or []:
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", None) not in {"verify_law_article", "search_law"}:
            continue
        artifact = getattr(msg, "artifact", None) or {}
        for cit in artifact.get("citations", []) or []:
            law = (cit.get("law_name") or "").strip()
            art = (cit.get("article_no") or "").strip()
            if not art:
                continue
            verified[(law, normalize_article_no(art))] = cit
    return verified


def _law_name_compatible(a: str, b: str) -> bool:
    """法名是否兼容：空则放过；否则去掉「中华人民共和国/书名号」后互为子串。

    用于把模型的简称（如「民法典」）匹配到法库全称（「中华人民共和国民法典」）。
    """
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return True
    norm = lambda s: s.replace("中华人民共和国", "").replace("《", "").replace("》", "")
    a2, b2 = norm(a), norm(b)
    return a2 in b2 or b2 in a2


def _sanitize_citations(
    raw_citations: list[ReviewCitation],
    verified: dict[tuple[str, str], dict[str, Any]],
) -> list[ReviewCitation]:
    """按 (law_name, article_no) 核验 LLM 提交的引用。

    - 命中已核实集合 → verified=True，以法库返回为准回填 citation_text / chunk_id。
    - 未命中 → verified=False，保留模型自填字段（chunk_id 空）。按用户取舍**不丢弃**，
      让前端区分「已核实/未核实」，避免丢掉库里没有但 LLM 正确的法条。
    - law_name + article_no 同时为空的引用才丢弃。
    """
    out: list[ReviewCitation] = []
    for cit in raw_citations or []:
        model_law = (cit.law_name or "").strip()
        model_article = (cit.article_no or "").strip()
        if not model_law and not model_article:
            continue

        norm = normalize_article_no(model_article)
        # 先按 (law_name, article_no) 精确命中
        rec = verified.get((model_law, norm))
        # 兜底：法名是简称/非规范时，按归一化条文号 + 兼容法名找唯一候选
        if rec is None and norm:
            candidates = [
                r for (l, a), r in verified.items()
                if a == norm and _law_name_compatible(model_law, l)
            ]
            if len(candidates) == 1:
                rec = candidates[0]

        if rec is not None:
            out.append(
                ReviewCitation(
                    law_name=rec.get("law_name", "") or model_law,
                    article_no=rec.get("article_no", "") or model_article,
                    citation_text=rec.get("citation_text", "") or cit.citation_text,
                    excerpt=cit.excerpt or (rec.get("content") or "")[:200],
                    chunk_id=rec.get("chunk_id", "") or "",
                    verified=True,
                )
            )
        else:
            logger.info("保留未核实引用 %s%s（未在法库核实集合中）", model_law, model_article)
            out.append(
                ReviewCitation(
                    law_name=model_law,
                    article_no=model_article,
                    citation_text=cit.citation_text,
                    excerpt=cit.excerpt,
                    chunk_id="",
                    verified=False,
                )
            )
    return out


def _submission_to_output(submission: ReviewSubmission) -> ReviewOutput:
    """把 submit_review 的 strict 参数转成业务 ReviewOutput。"""
    return ReviewOutput(
        has_opinion=submission.has_opinion,
        opinions=[
            ReviewOpinion(
                opinion_type=opinion.opinion_type,
                review_dimension=opinion.review_dimension,
                finding=opinion.finding,
                recommendation=opinion.recommendation,
                confidence=opinion.confidence,
                citations=[
                    ReviewCitation(
                        law_name=c.law_name,
                        article_no=c.article_no,
                        citation_text=c.citation_text,
                        excerpt=c.excerpt,
                    )
                    for c in opinion.citations
                ],
            )
            for opinion in submission.opinions
        ],
        risk_assessment=ClauseRiskAssessment(
            risk_level=submission.risk_assessment.risk_level,
            rationale=submission.risk_assessment.rationale,
            affected_party=submission.risk_assessment.affected_party,
            confidence=submission.risk_assessment.confidence,
        ),
        consistency_facts=[
            ConsistencyFact(
                category=f.category,
                key=f.key,
                party=f.party,
                value_text=f.value_text,
                normalized_value=f.normalized_value,
                span_text=f.span_text,
                related_text=f.related_text,
                confidence=f.confidence,
            )
            for f in submission.consistency_facts
        ],
        note=submission.note,
    )


def _coerce_review_output(raw: Any) -> ReviewOutput:
    """把 submit_review artifact 或 tool call args 转成 ReviewOutput。"""
    if isinstance(raw, ReviewOutput):
        return raw
    if isinstance(raw, ReviewSubmission):
        return _submission_to_output(raw)
    if isinstance(raw, dict):
        payload = raw.get("review_output") if isinstance(raw.get("review_output"), dict) else raw
        return _submission_to_output(ReviewSubmission.model_validate(payload))
    raise TypeError(f"unsupported review output payload: {type(raw)!r}")


def _extract_submitted_review(messages: list) -> ReviewOutput | None:
    """从执行轨迹里提取 submit_review 提交的最终结果。"""
    for msg in reversed(messages or []):
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", None) != "submit_review":
            continue
        artifact = getattr(msg, "artifact", None)
        if artifact is None:
            continue
        try:
            return _coerce_review_output(artifact)
        except Exception as exc:
            logger.warning("submit_review artifact 解析失败: %s", exc)

    for msg in reversed(messages or []):
        for tool_call in reversed(getattr(msg, "tool_calls", []) or []):
            if tool_call.get("name") != "submit_review":
                continue
            try:
                return _coerce_review_output(tool_call.get("args") or {})
            except Exception as exc:
                logger.warning("submit_review tool_call args 解析失败: %s", exc)
    return None


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def _format_user_prompt(
    *,
    contract_title: str,
    section_path: str,
    clause_no: str,
    clause_text: str,
    party_stance: str = "未知",
    focus_dimensions: list[str] | None = None,
) -> str:
    """把条款上下文打包成给 Agent 的 user message（含委托人立场与重点审查维度）。"""
    head = f"合同：{contract_title}".strip()
    sect = f"章节：{section_path}".strip() if section_path else ""
    cno = f"条款编号：{clause_no}".strip() if clause_no else ""
    if party_stance in ("甲方", "乙方", "中立"):
        stance_line = f"委托人立场：{party_stance}（请站在该方利益审查，优先识别对其不利/权责不对等之处）"
    else:
        stance_line = "委托人立场：未知（按中立口径审查，对不对等之处双向标注更不利的一方）"
    parts = [p for p in (head, sect, cno, stance_line) if p]
    if focus_dimensions:
        parts.append("本条款重点审查维度：" + "、".join(focus_dimensions))
    header = "\n".join(parts)
    return (
        f"{header}\n\n"
        f"请审查以下合同条款，并通过 submit_review 工具提交审查意见、条款级风险评估和一致性事实：\n\n"
        f"```\n{clause_text}\n```"
    )


def _chunk_text(content: Any) -> str:
    """从 AIMessageChunk.content 提取正文（排除 thinking/reasoning 块）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") in ("thinking", "reasoning"):
                    continue
                t = b.get("text")
                if isinstance(t, str):
                    parts.append(t)
            elif isinstance(b, str):
                parts.append(b)
        return "".join(parts)
    return ""


def _chunk_thinking(content: Any) -> str:
    """从 content-block 提取 thinking/reasoning 文本。"""
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict) and b.get("type") in ("thinking", "reasoning"):
                parts.append(b.get("text") or b.get("thinking") or "")
        return "".join(parts)
    return ""


def _jsonable_trace_value(value: Any) -> Any:
    """把工具 artifact 转成可 JSON 序列化的评测 trace 值。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable_trace_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable_trace_value(v) for v in value]
    if hasattr(value, "model_dump"):
        return _jsonable_trace_value(value.model_dump())
    return str(value)


def _bounded_trace_artifact(artifact: Any, max_chars: int) -> Any:
    """限制工具 artifact 的体积，避免评测 trace 文件失控。"""
    if artifact is None:
        return None
    jsonable = _jsonable_trace_value(artifact)
    raw = json.dumps(jsonable, ensure_ascii=False, sort_keys=True)
    if len(raw) <= max_chars:
        return jsonable
    return {
        "_truncated": True,
        "original_chars": len(raw),
        "preview": raw[:max_chars],
    }


def _finalize_review(
    submission: ReviewOutput,
    verified: dict[tuple[str, str], dict[str, Any]],
) -> ReviewOutput:
    """对提交结果做 citation 防幻觉清洗，得到最终 ReviewOutput。

    调用方保证 ``submission`` 非空：agent 漏调 submit_review 时先强制补提交，
    补提交仍失败则抛 :class:`ClauseReviewNotSubmittedError`，不会走到这里。
    """
    cleaned_opinions: list[ReviewOpinion] = []
    for r in submission.opinions:
        cleaned_opinions.append(
            ReviewOpinion(
                opinion_type=r.opinion_type,
                review_dimension=r.review_dimension,
                finding=r.finding,
                recommendation=r.recommendation,
                confidence=r.confidence,
                citations=_sanitize_citations(r.citations, verified),
            )
        )
    return ReviewOutput(
        has_opinion=submission.has_opinion and len(cleaned_opinions) > 0,
        opinions=cleaned_opinions,
        risk_assessment=submission.risk_assessment,
        consistency_facts=submission.consistency_facts,
        note=submission.note,
    )


def _build_force_submit_model():
    """构建用于「强制补提交」的模型：绑定 submit_review 并强制 tool_choice。

    DeepSeek 等模型在「无意见」条款上常直接用文本收尾、漏调 submit_review。此时不再多轮
    ReAct，而是关掉 thinking、用 tool_choice 逼模型把已有分析一次性结构化提交（更快更确定）。
    """
    if settings.llm_provider.lower() == "deepseek":
        model = get_chat_llm(
            enable_thinking=False,
            base_url=settings.deepseek_beta_base_url,
            timeout=settings.llm_review_timeout,
        )
        return model.bind_tools([submit_review], tool_choice="submit_review", strict=True)
    model = get_chat_llm(timeout=settings.llm_review_timeout)
    return model.bind_tools([submit_review], tool_choice="submit_review")


async def _force_submit_review(
    *,
    user_msg: str,
    final_message: Any,
    run_config: dict[str, Any] | None,
) -> ReviewOutput | None:
    """agent 漏调 submit_review 时的强制补提交：救回它已经做出的判断。

    把原始条款 prompt + agent 的文本结论 + 一条「立刻调用 submit_review」的指令喂回模型，
    强制其产出结构化结果。成功返回 ReviewOutput；仍拿不到合法 tool call 时返回 None
    （由上层抛 ClauseReviewNotSubmittedError 走失败路径，绝不静默当无风险）。
    """
    try:
        forced = _build_force_submit_model()
    except Exception as exc:
        logger.warning("构建强制补提交模型失败: %s", exc)
        return None

    # 取 agent 最后一条输出的「思考 + 正文」作为上下文回传：风险结论常落在 thinking 块里
    # （如思考判 high）。若只回传正文，补提交模型会在缺少结论的情况下重新裸判，导致最终
    # risk_level 与思考过程不一致（思考 high、补提交却给 low）。这里把 thinking 转成纯文本一并
    # 回传（不作为 reasoning_content 块，避免 DeepSeek beta 报 400），并要求模型严格沿用既有结论。
    prior = ""
    if final_message is not None:
        content = getattr(final_message, "content", "")
        think = _chunk_thinking(content) or (
            getattr(final_message, "additional_kwargs", {}) or {}
        ).get("reasoning_content", "")
        text = _chunk_text(content)
        prior = "\n\n".join(p.strip() for p in (think, text) if p and p.strip())
    messages: list[Any] = [HumanMessage(content=user_msg)]
    if prior.strip():
        messages.append(AIMessage(content=prior))
    messages.append(
        HumanMessage(
            content=(
                "你刚才已完成对该条款的分析（见上一条你的分析与结论），但没有通过 submit_review 提交结构化结果。"
                "请严格沿用你上面已得出的结论提交，尤其 risk_assessment.risk_level 必须与你的分析结论保持一致，"
                "不要重新评估、不要无故降级或升级；逐条把审查意见、条款级风险评估与一致性事实结构化提交。"
                '若你上面的结论确为无意见，则 has_opinion=false、opinions=[]、risk_assessment.risk_level="none"，'
                "并在 note 简述判断依据。不要再输出普通文本。"
            )
        )
    )

    try:
        resp = await forced.ainvoke(messages, config=run_config)
    except Exception as exc:
        logger.warning("强制补提交 submit_review 调用失败: %s", exc)
        return None

    for tc in getattr(resp, "tool_calls", None) or []:
        if tc.get("name") != "submit_review":
            continue
        try:
            return _coerce_review_output(tc.get("args") or {})
        except Exception as exc:
            logger.warning("强制补提交 submit_review args 解析失败: %s", exc)
            return None
    logger.warning("强制补提交未产出 submit_review tool call")
    return None


async def areview_clause_events(
    *,
    contract_title: str,
    section_path: str,
    clause_no: str,
    clause_text: str,
    party_stance: str = "未知",
    focus_dimensions: list[str] | None = None,
    run_config: dict[str, Any] | None = None,
    include_tool_artifact: bool = False,
    max_tool_artifact_chars: int = 20000,
) -> AsyncIterator[dict[str, Any]]:
    """流式审查一条合同条款，逐事件 yield 供上层转 SSE。

    ``run_config``：传给 agent 的 LangChain config（metadata/tags/run_name），用于在 LangSmith 里
    给本条款的 trace 打标（如 clause_id）。可选，默认 None；嵌套在审查图节点内调用时会自动挂到父 run。
    ``include_tool_artifact``：仅供离线评测开启，用于把工具检索 artifact 放入 trace；默认关闭以保持线上 SSE 行为。

    事件形状（不含 clause_id，由上层 fan-in 时打标）：
    - {"type": "think",  "delta": str}                       审查 agent 推理增量
    - {"type": "tool_start", "name", "args", "call_id"}      core 法库工具调用开始
    - {"type": "tool_end",   "name", "result_preview", "citations", "call_id"}
    - {"type": "result", "review": ReviewOutput}             最终清洗后的结构化结果（恒在最后）

    注：不产出 content/正文 增量。review agent 的正式输出只走 submit_review 工具，
    content 通道是工具间的规划碎片（噪声），故不对外暴露。

    submit_review 工具不作为可见步骤暴露；其 artifact 用于得到最终结果。
    """
    if not clause_text.strip():
        yield {
            "type": "result",
            "review": ReviewOutput(
                has_opinion=False,
                opinions=[],
                risk_assessment=ClauseRiskAssessment(
                    risk_level="none",
                    rationale="条款为空",
                    affected_party="不适用",
                    confidence=1.0,
                ),
                consistency_facts=[],
                note="条款为空",
            ),
        }
        return

    agent = get_review_agent()
    user_msg = _format_user_prompt(
        contract_title=contract_title,
        section_path=section_path,
        clause_no=clause_no,
        clause_text=clause_text,
        party_stance=party_stance,
        focus_dimensions=focus_dimensions,
    )

    # 优先按事件名直接捕获（不依赖 on_tool_end output 是否带 .name）；
    # 同时收集 ToolMessage，作为提取逻辑的兜底，并保持 helper 被复用。
    tool_messages: list[Any] = []
    submission_direct: ReviewOutput | None = None
    verified_direct: dict[tuple[str, str], dict[str, Any]] = {}
    final_ai_message: Any = None  # 最后一条模型输出，供漏调 submit_review 时强制补提交参考

    # 异常不再吞掉：原先这里 try/except 兜住所有异常并 yield 空 ReviewOutput，
    # 导致 review_clause 节点把失败条款误标 review_status="done"、failed_count 永远为 0。
    # review_clause 节点本身已经包了 try/except（不会回滚 superstep），由它统一标 failed。
    async for ev in agent.astream_events(
        {"messages": [HumanMessage(content=user_msg)]},
        version="v2",
        config=run_config,
    ):
        etype = ev.get("event")
        data = ev.get("data") or {}

        if etype == "on_chat_model_stream":
            chunk = data.get("chunk")
            if chunk is None:
                continue
            if getattr(chunk, "tool_call_chunks", None) or getattr(chunk, "tool_calls", None):
                continue
            content = getattr(chunk, "content", "")
            think = _chunk_thinking(content) or (
                getattr(chunk, "additional_kwargs", {}) or {}
            ).get("reasoning_content", "")
            if think:
                yield {"type": "think", "delta": think}
            # 不再 yield content 通道：review agent 的正式产出只通过 submit_review 工具，
            # prompt 也禁止用普通文本收尾。其 content 通道是模型在工具间吐的规划碎片（关键词），
            # 属噪声，曾被前端高亮成「模型输出」误导用户，故从源头丢弃，只保留 think + 工具 + 最终结论。

        elif etype == "on_chat_model_end":
            out = data.get("output")
            if out is not None:
                final_ai_message = out

        elif etype == "on_tool_start":
            name = ev.get("name") or "tool"
            if name == "submit_review":
                continue
            input_value = data.get("input") or {}
            args = input_value if isinstance(input_value, dict) else {"input": input_value}
            yield {
                "type": "tool_start",
                "name": name,
                "args": args,
                "call_id": ev.get("run_id") or "",
            }

        elif etype == "on_tool_end":
            name = ev.get("name") or "tool"
            output = data.get("output")
            if output is not None:
                tool_messages.append(output)
            artifact = getattr(output, "artifact", None)

            if name == "submit_review":
                if isinstance(artifact, dict):
                    try:
                        submission_direct = _coerce_review_output(artifact)
                    except Exception as exc:
                        logger.warning("submit_review artifact 解析失败: %s", exc)
                continue

            citations: list[dict[str, Any]] = []
            if isinstance(artifact, dict):
                citations = list(artifact.get("citations") or [])
                for cit in citations:
                    art = (cit.get("article_no") or "").strip()
                    if art:
                        law = (cit.get("law_name") or "").strip()
                        verified_direct[(law, normalize_article_no(art))] = cit

            content = getattr(output, "content", output)
            preview = content if isinstance(content, str) else str(content or "")
            event = {
                "type": "tool_end",
                "name": name,
                "result_preview": preview[:8000],
                "citations": citations,
                "call_id": ev.get("run_id") or "",
            }
            if include_tool_artifact:
                event["artifact"] = _bounded_trace_artifact(
                    artifact,
                    max(1000, max_tool_artifact_chars),
                )
            yield event

    submission = submission_direct or _extract_submitted_review(tool_messages)
    verified = verified_direct or _collect_verified_articles(tool_messages)

    if submission is None:
        # 模型跑完一整轮却没调用 submit_review（DeepSeek 在「无意见」条款上常见）。
        # 不静默当无风险：先强制补提交救回它已经做出的判断。
        logger.warning(
            "条款审查未调用 submit_review，尝试强制补提交 clause_no=%r", clause_no,
        )
        submission = await _force_submit_review(
            user_msg=user_msg,
            final_message=final_ai_message,
            run_config=run_config,
        )
        if submission is not None:
            logger.info("强制补提交 submit_review 成功 clause_no=%r", clause_no)

    if submission is None:
        # 强制补提交仍失败：抛错走 review_clause 的失败路径（标 failed + 计入 failed_clauses）。
        raise ClauseReviewNotSubmittedError(
            f"review agent 未提交结构化审查结果（含强制补提交），clause_no={clause_no!r}"
        )

    yield {"type": "result", "review": _finalize_review(submission, verified)}


__all__ = [
    "areview_clause_events",
    "get_review_agent",
    "build_review_agent",
    "ClauseReviewNotSubmittedError",
]
