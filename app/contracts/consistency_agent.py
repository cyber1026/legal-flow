"""合同一致性审查 Agent。

在所有条款审查完成后运行，基于条款级审查意见、条款级风险评估和一致性事实
做合同级横向比对。输出的一致性意见不绑定风险等级；整体风险由独立
risk_assessment 表达。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict, Field

from app.contracts.prompts.risk_schema import (
    AffectedParty,
    ConsistencyOpinion,
    ConsistencyReviewOutput,
    ContractConsistencyRiskAssessment,
    OpinionType,
    ReviewDimension,
    RiskLevel,
)
from app.core.config import settings
from app.llm.factory import get_chat_llm

logger = logging.getLogger(__name__)


class ConsistencyReviewNotSubmittedError(RuntimeError):
    """一致性 agent 跑完仍未提交结构化结果（含强制补提交也失败）。

    由 consistency_review 节点的 try/except 捕获并把一致性审查标为 failed，
    绝不再静默伪装成 risk_level="none"——与条款级审查同一纪律。
    """


class ConsistencyOpinionSubmission(BaseModel):
    """一致性审查的单条意见；不包含风险等级。

    extra="ignore"：DeepSeek 偶尔在严格模式外多塞字段，宽容丢弃而非让整次提交失败；
    confidence 给默认值：模型常漏填该软指标，缺失时兜默认而非 7 连校验错误。
    """

    # 字段一律必填（不给默认）：consistency agent 走 DeepSeek strict，strict 要求每个对象的
    # required 覆盖全部属性，否则 API 直接 400；漏填问题靠 strict 服务端强制解决，而非默认值兜底。
    model_config = ConfigDict(extra="ignore")

    opinion_type: OpinionType
    review_dimension: ReviewDimension
    finding: str
    recommendation: str
    related_clause_ids: list[str] = Field(description="涉及的 clause_id 列表；没有则填空数组")
    # list[str] 而非 list[dict]：后者在 strict 模式生成「无属性 object」会被 DeepSeek API 拒（400）。
    evidence_facts: list[str] = Field(description="支撑该意见的一致性事实摘要，每条一句话；没有则填空数组")
    confidence: float = Field(ge=0.0, le=1.0, description="判断置信度 0~1")


class ContractConsistencyRiskAssessmentSubmission(BaseModel):
    """合同一致性层面的整体风险评估。"""

    model_config = ConfigDict(extra="ignore")

    risk_level: RiskLevel
    rationale: str
    affected_party: AffectedParty
    confidence: float = Field(ge=0.0, le=1.0, description="判断置信度 0~1")


class ConsistencyReviewSubmission(BaseModel):
    """submit_consistency_review 的完整参数。"""

    model_config = ConfigDict(extra="ignore")

    has_opinion: bool
    opinions: list[ConsistencyOpinionSubmission]
    risk_assessment: ContractConsistencyRiskAssessmentSubmission
    note: str


@tool(
    "submit_consistency_review",
    args_schema=ConsistencyReviewSubmission,
    return_direct=True,
    response_format="content_and_artifact",
)
def submit_consistency_review(**kwargs: Any) -> tuple[str, dict[str, Any]]:
    """提交合同一致性审查结果；这是唯一合法的最终输出方式。"""
    submission = ConsistencyReviewSubmission.model_validate(kwargs)
    return "一致性审查结果已提交", {"consistency_review_output": submission.model_dump()}


CONSISTENCY_SYSTEM_PROMPT = """你是一名合同一致性审查律师 AI，负责在所有条款审查完成后做全合同横向比对。

你只基于用户消息提供的条款目录、条款级审查意见、条款级风险评估和一致性事实判断，不得编造未提供的事实。

重点检查：
1. 主体名称、简称、身份、签约方前后是否一致。
2. 定义术语、金额、付款期限、履行期限、合同期限是否冲突。
3. 权利义务与违约责任、解除条件、通知送达、管辖约定是否对应。
4. 附件引用、条款交叉引用是否缺失、冲突或无法定位。
5. 是否存在同一事项前后表述不一，导致履行或追责困难。

输出规则：
- 必须调用 submit_consistency_review，禁止普通文本作最终输出。
- 一致性 opinions 不包含 risk_level；每条意见只写 finding/recommendation。
- risk_assessment 是一致性层面的整体风险评估，独立于单条意见。
- 所有枚举必须单选，禁止「疑问/警告」「none/low」等候选值。
- 事实不足时用「疑问」或「提醒」，不要硬判高风险。
"""


def _submission_to_output(submission: ConsistencyReviewSubmission) -> ConsistencyReviewOutput:
    return ConsistencyReviewOutput(
        has_opinion=submission.has_opinion,
        opinions=[
            ConsistencyOpinion(
                opinion_type=o.opinion_type,
                review_dimension=o.review_dimension,
                finding=o.finding,
                recommendation=o.recommendation,
                related_clause_ids=o.related_clause_ids,
                evidence_facts=o.evidence_facts,
                confidence=o.confidence,
            )
            for o in submission.opinions
        ],
        risk_assessment=ContractConsistencyRiskAssessment(
            risk_level=submission.risk_assessment.risk_level,
            rationale=submission.risk_assessment.rationale,
            affected_party=submission.risk_assessment.affected_party,
            confidence=submission.risk_assessment.confidence,
        ),
        note=submission.note,
    )


def _coerce_consistency_output(raw: Any) -> ConsistencyReviewOutput:
    if isinstance(raw, ConsistencyReviewOutput):
        return raw
    if isinstance(raw, ConsistencyReviewSubmission):
        return _submission_to_output(raw)
    if isinstance(raw, dict):
        payload = (
            raw.get("consistency_review_output")
            if isinstance(raw.get("consistency_review_output"), dict)
            else raw
        )
        return _submission_to_output(ConsistencyReviewSubmission.model_validate(payload))
    raise TypeError(f"unsupported consistency output payload: {type(raw)!r}")


def _extract_submitted_consistency(messages: list) -> ConsistencyReviewOutput | None:
    for msg in reversed(messages or []):
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", None) != "submit_consistency_review":
            continue
        artifact = getattr(msg, "artifact", None)
        if artifact is None:
            continue
        try:
            return _coerce_consistency_output(artifact)
        except Exception as exc:
            logger.warning("submit_consistency_review artifact 解析失败: %s", exc)

    for msg in reversed(messages or []):
        for tool_call in reversed(getattr(msg, "tool_calls", []) or []):
            if tool_call.get("name") != "submit_consistency_review":
                continue
            try:
                return _coerce_consistency_output(tool_call.get("args") or {})
            except Exception as exc:
                logger.warning("submit_consistency_review tool_call args 解析失败: %s", exc)
    return None


def _get_consistency_model():
    """consistency agent 模型：与 review agent 同构——DeepSeek 用 beta 端点 + 长超时，
    配合 DeepSeekStrictToolMiddleware 走 strict，确保 submit_consistency_review 参数严格符合 schema
    （strict 服务端强制全字段，根治模型漏填 confidence 等导致整次提交校验失败的问题）。"""
    if settings.llm_provider.lower() == "deepseek":
        return get_chat_llm(
            enable_thinking=True,
            base_url=settings.deepseek_beta_base_url,
            timeout=settings.llm_review_timeout,
        )
    return get_chat_llm(timeout=settings.llm_review_timeout)


@lru_cache(maxsize=1)
def get_consistency_agent() -> CompiledStateGraph:
    # 与 review agent 一致：DeepSeek 下挂 strict 中间件，让 submit_consistency_review 参数被服务端严格校验。
    middleware = []
    if settings.llm_provider.lower() == "deepseek":
        from app.contracts.review_agent import DeepSeekStrictToolMiddleware
        middleware.append(DeepSeekStrictToolMiddleware())
    return create_agent(
        model=_get_consistency_model(),
        tools=[submit_consistency_review],
        system_prompt=CONSISTENCY_SYSTEM_PROMPT,
        middleware=middleware,
    )


def build_consistency_prompt(payload: dict[str, Any]) -> str:
    return (
        "请基于以下 JSON 做合同一致性审查，并通过 submit_consistency_review 提交结果。\n"
        "JSON 中的 facts 是单条款 agent 抽取的可比对事实，opinions 是条款级审查意见，"
        "clause_risk_assessments 是条款级综合风险评估。\n\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```"
    )


def _build_force_submit_model():
    """构建用于「强制补提交」的模型：绑定 submit_consistency_review 并强制 tool_choice。

    与条款级 review_agent 同一套路：DeepSeek 在「无一致性问题」时常直接用文本收尾、漏调工具。
    此时关掉 thinking、用 tool_choice 逼模型把已有分析一次性结构化提交（更快更确定）。
    """
    if settings.llm_provider.lower() == "deepseek":
        model = get_chat_llm(
            enable_thinking=False,
            base_url=settings.deepseek_beta_base_url,
            timeout=settings.llm_review_timeout,
        )
        return model.bind_tools(
            [submit_consistency_review], tool_choice="submit_consistency_review", strict=True
        )
    model = get_chat_llm(timeout=settings.llm_review_timeout)
    return model.bind_tools([submit_consistency_review], tool_choice="submit_consistency_review")


def _last_ai_analysis(messages: list) -> str:
    """取最后一条 AIMessage 的「思考 + 正文」（纯文本拼接）。

    一致性风险结论常落在 thinking 块里；补提交若只回传正文会让模型重新裸判、与思考过程不一致
    （思考判 high、补提交却给 low）。这里把 thinking 一并转纯文本回传（不作为 reasoning_content 块，
    避免 DeepSeek beta 报 400）。
    """
    for msg in reversed(messages or []):
        if not isinstance(msg, AIMessage):
            continue
        content = getattr(msg, "content", "")
        if isinstance(content, str):
            text, think = content, ""
        elif isinstance(content, list):
            text_parts: list[str] = []
            think_parts: list[str] = []
            for b in content:
                if isinstance(b, dict):
                    if b.get("type") in ("thinking", "reasoning"):
                        think_parts.append(b.get("text") or b.get("thinking") or "")
                    else:
                        t = b.get("text")
                        if isinstance(t, str):
                            text_parts.append(t)
                elif isinstance(b, str):
                    text_parts.append(b)
            text, think = "".join(text_parts), "".join(think_parts)
        else:
            text, think = "", ""
        if not think:
            think = (getattr(msg, "additional_kwargs", {}) or {}).get("reasoning_content", "") or ""
        return "\n\n".join(p.strip() for p in (think, text) if p and p.strip())
    return ""


async def _force_submit_consistency(
    *,
    prompt: str,
    prior_text: str,
    run_config: dict[str, Any] | None,
) -> ConsistencyReviewOutput | None:
    """agent 漏调 submit_consistency_review 时的强制补提交：救回它已经做出的判断。

    成功返回 ConsistencyReviewOutput；仍拿不到合法 tool call 时返回 None
    （由上层抛 ConsistencyReviewNotSubmittedError 走失败路径，绝不静默当无风险）。
    """
    try:
        forced = _build_force_submit_model()
    except Exception as exc:
        logger.warning("构建一致性强制补提交模型失败: %s", exc)
        return None

    messages: list[Any] = [HumanMessage(content=prompt)]
    if prior_text.strip():
        messages.append(AIMessage(content=prior_text))
    messages.append(
        HumanMessage(
            content=(
                "你刚才已完成一致性分析（见上一条你的分析与结论），但没有通过 submit_consistency_review 提交结构化结果。"
                "请严格沿用你上面已得出的结论提交，尤其 risk_assessment.risk_level 必须与你的分析结论保持一致，"
                "不要重新评估、不要无故降级或升级。"
                "若你上面的结论确为无一致性问题，则 has_opinion=false、"
                'opinions=[]、risk_assessment.risk_level="none"，并在 note 简述判断依据。'
                "不要再输出普通文本。"
            )
        )
    )

    try:
        resp = await forced.ainvoke(messages, config=run_config)
    except Exception as exc:
        logger.warning("一致性强制补提交调用失败: %s", exc)
        return None

    for tc in getattr(resp, "tool_calls", None) or []:
        if tc.get("name") != "submit_consistency_review":
            continue
        try:
            return _coerce_consistency_output(tc.get("args") or {})
        except Exception as exc:
            logger.warning("一致性强制补提交 args 解析失败: %s", exc)
            return None
    logger.warning("一致性强制补提交未产出 submit_consistency_review tool call")
    return None


async def areview_consistency(payload: dict[str, Any], run_config: dict[str, Any] | None = None) -> ConsistencyReviewOutput:
    if not payload.get("consistency_facts"):
        return ConsistencyReviewOutput(
            has_opinion=False,
            opinions=[],
            risk_assessment=ContractConsistencyRiskAssessment(
                risk_level="none",
                rationale="未抽取到可供一致性比对的事实",
                affected_party="不适用",
                confidence=1.0,
            ),
            note="未抽取到可供一致性比对的事实",
        )
    agent = get_consistency_agent()
    prompt = build_consistency_prompt(payload)
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=prompt)]},
        config=run_config,
    )
    messages = result.get("messages") or []
    submitted = _extract_submitted_consistency(messages)

    if submitted is None:
        # 模型跑完却没调用 submit_consistency_review（DeepSeek 在「无一致性问题」时常见）。
        # 不静默当无风险：先强制补提交救回它已经做出的判断。
        logger.warning(
            "一致性审查未调用 submit_consistency_review，尝试强制补提交 facts=%d",
            len(payload.get("consistency_facts") or []),
        )
        submitted = await _force_submit_consistency(
            prompt=prompt,
            prior_text=_last_ai_analysis(messages),
            run_config=run_config,
        )
        if submitted is not None:
            logger.info("一致性强制补提交 submit_consistency_review 成功")

    if submitted is None:
        # 强制补提交仍失败：抛错走 consistency_review 节点的失败路径，绝不伪装无风险。
        raise ConsistencyReviewNotSubmittedError(
            "一致性审查 Agent 未提交结构化结果（含强制补提交）"
        )

    return submitted


async def areview_consistency_events(
    payload: dict[str, Any], run_config: dict[str, Any] | None = None
) -> AsyncIterator[dict[str, Any]]:
    """流式合同一致性审查，逐事件 yield 供上层转 SSE（与 areview_clause_events 同构）。

    事件形状：
    - {"type": "think",  "delta": str}                          一致性 agent 推理增量
    - {"type": "result", "review": ConsistencyReviewOutput}     最终结构化结果（恒在最后）

    一致性 agent 只有 submit_consistency_review 一个工具（无法库工具），故不产出工具步骤；
    展示给前端的「思考过程」即模型的 think 增量。content/正文 通道是噪声，不对外暴露。
    与 areview_consistency（ainvoke 版，测试与非流式兜底用）并存，避免破坏既有打桩。
    """
    # 延迟导入：复用 review_agent 的 thinking 解析，避免顶层循环依赖。
    from app.contracts.review_agent import _chunk_thinking

    if not payload.get("consistency_facts"):
        yield {
            "type": "result",
            "review": ConsistencyReviewOutput(
                has_opinion=False,
                opinions=[],
                risk_assessment=ContractConsistencyRiskAssessment(
                    risk_level="none",
                    rationale="未抽取到可供一致性比对的事实",
                    affected_party="不适用",
                    confidence=1.0,
                ),
                note="未抽取到可供一致性比对的事实",
            ),
        }
        return

    agent = get_consistency_agent()
    prompt = build_consistency_prompt(payload)
    tool_messages: list[Any] = []
    submitted_direct: ConsistencyReviewOutput | None = None
    final_ai_message: Any = None

    async for ev in agent.astream_events(
        {"messages": [HumanMessage(content=prompt)]},
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
            # 不再 yield content 通道：一致性 agent 的正式产出只通过 submit_consistency_review 工具，
            # content 是工具前后的规划碎片（噪声），与条款级审查同一处理——只保留 think + 最终结论。

        elif etype == "on_chat_model_end":
            out = data.get("output")
            if out is not None:
                final_ai_message = out

        elif etype == "on_tool_end":
            output = data.get("output")
            if output is not None:
                tool_messages.append(output)
            artifact = getattr(output, "artifact", None)
            if ev.get("name") == "submit_consistency_review" and isinstance(artifact, dict):
                try:
                    submitted_direct = _coerce_consistency_output(artifact)
                except Exception as exc:
                    logger.warning("submit_consistency_review artifact 解析失败: %s", exc)

    submitted = submitted_direct or _extract_submitted_consistency(tool_messages)
    if submitted is None:
        # 漏调 submit_consistency_review：强制补提交救回已做出的判断，绝不静默当无风险。
        logger.warning("一致性审查未调用 submit_consistency_review（流式），尝试强制补提交")
        prior = _last_ai_analysis([final_ai_message] if final_ai_message is not None else [])
        submitted = await _force_submit_consistency(
            prompt=prompt, prior_text=prior, run_config=run_config
        )
        if submitted is not None:
            logger.info("一致性强制补提交 submit_consistency_review 成功")
    if submitted is None:
        raise ConsistencyReviewNotSubmittedError(
            "一致性审查 Agent 未提交结构化结果（含强制补提交）"
        )
    yield {"type": "result", "review": submitted}


__all__ = [
    "ConsistencyReviewSubmission",
    "ConsistencyOpinionSubmission",
    "ContractConsistencyRiskAssessmentSubmission",
    "submit_consistency_review",
    "areview_consistency",
    "areview_consistency_events",
    "get_consistency_agent",
    "ConsistencyReviewNotSubmittedError",
]
