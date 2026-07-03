"""审查支撑知识库的「检索 + 总结」工具工厂（5 库各产出一个按需工具）。

每个工具 = 轻量链：向量检索该库 top-k → 抽取当前待审条款上下文 → 用**快模型**（不开 thinking）做一次
总结，萃取「哪些内容对本条款审查有用」→ 返回 digest。挂到审查 agent（与 law_tools 同构），由审查 LLM
按条款自行决定调哪几个。

设计取舍（用户确认）：
- 按需工具：审查 LLM 自主选调，不每条款都跑全部 5 库，成本可控。
- 轻量链 + 快模型：单次检索 + 单次总结，``settings.kb_summarize_model`` 可指定更快的模型；空召回直接短路不调 LLM。
- 语义边界：这些是**审查参照材料**（示范条款 / 裁判规则 / 实务立场），不是法条；正式法律引用仍只走
  law_tools 的 verify_law_article / search_law。工具描述与审查 prompt 都强调这一点。
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from functools import lru_cache
from typing import Annotated, Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from app.core.config import settings
from app.knowledge.registry import KB_REGISTRY, KBSpec
from app.knowledge.retriever import KBRetriever
from app.llm.factory import get_chat_llm

logger = logging.getLogger(__name__)

KB_SUMMARIZE_TAG = "kb_summarize"


class KBToolTimeoutError(TimeoutError):
    """支撑库工具内部步骤超时。"""


def _run_with_timeout(label: str, timeout_s: float, fn):
    """同步步骤超时保护，避免 KB 工具无期限阻塞而不产生 tool_end。"""
    timeout_s = max(float(timeout_s or 0), 0.1)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"kb-{label}")
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout_s)
    except FutureTimeout as exc:
        future.cancel()
        raise KBToolTimeoutError(f"{label} 超过 {timeout_s:.0f}s 未返回") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# 快模型（总结用）
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _get_summarize_llm() -> BaseChatModel:
    """总结用的快模型单例：低温、不开 thinking，尽量低延迟低成本。

    ``settings.kb_summarize_model`` 为空时用当前 provider 默认模型；可在 .env 指定更快的模型名。
    ``enable_thinking=False`` 对 DeepSeek 生效（关思考），其它 provider 静默忽略。
    """
    return get_chat_llm(
        model=settings.kb_summarize_model or None,
        temperature=0.0,
        enable_thinking=False,
    )


# ---------------------------------------------------------------------------
# 上下文抽取 / 结果格式化
# ---------------------------------------------------------------------------

def _content_to_text(content: Any) -> str:
    """从消息 content 提取纯文本（兼容 content-block 列表，跳过 thinking/reasoning 块）。"""
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


def _extract_clause_context(state: Any) -> str:
    """从会话状态首条 HumanMessage 抽取当前待审条款上下文（best-effort）。

    审查 agent 的入口 prompt（``areview_clause_events`` 构造）首条就是「合同/章节/条款 + 正文」；
    supervisor 场景没有条款时，这里取到的是用户消息，总结链退化为「仅按 query」也能用。
    """
    msgs = state.get("messages") if isinstance(state, dict) else getattr(state, "messages", None)
    if not msgs:
        return ""
    for m in msgs:
        # 用类名判断，避免对 message 类型做硬 import 耦合
        if type(m).__name__ in ("HumanMessage", "HumanMessageChunk"):
            text = _content_to_text(getattr(m, "content", ""))
            return text.strip()[:2000]
    return ""


def _result_label(r: dict[str, Any]) -> str:
    """为一条检索结果取一个可读标题（各库字段不同，按优先级兜底）。"""
    if r.get("kb_type") == "case_rule" and r.get("title"):
        return str(r["title"])
    if str(r.get("kb_type") or "").startswith("judicial_") and r.get("source_title"):
        article_no = str(r.get("article_no") or "").strip()
        return f"{r['source_title']} {article_no}".strip()
    for key in ("citation", "citation_text", "title", "contract_title", "clause_title"):
        v = r.get(key)
        if v:
            return str(v)
    return str(r.get("chunk_id") or "(无标题)")


def _result_source(r: dict[str, Any], label: str) -> str:
    """给总结模型看的来源标签。案例优先展示案件名和日期，其他库保留原链接/ID。"""
    if r.get("kb_type") == "case_rule":
        date = r.get("publish_time") or r.get("publish_date") or ""
        return f"{label}（{date}）" if date else label
    if str(r.get("kb_type") or "").startswith("judicial_") and r.get("source_title"):
        return label
    return str(r.get("source_url") or r.get("chunk_id") or "")


def _case_cites_text(r: dict[str, Any]) -> str:
    """把案例 metadata 里的 cites 渲染成给总结模型看的法条列表。"""
    cites = r.get("cites") or []
    if isinstance(cites, str):
        try:
            parsed = json.loads(cites)
            cites = parsed if isinstance(parsed, list) else []
        except Exception:
            cites = []
    parts: list[str] = []
    if isinstance(cites, list):
        for c in cites:
            if not isinstance(c, dict):
                continue
            law = str(c.get("law") or "").strip()
            article = str(c.get("article") or "").strip()
            text = f"{law}{article}" if law else article
            if text:
                parts.append(text)
    return "、".join(parts)


def _render_field(value: Any) -> str:
    """渲染一个 content 字段：JSON 序列化的 list（如 contract_outline）→ 逐行；标量 → 原样。"""
    s = str(value).strip()
    if s.startswith("[") and s.endswith("]"):
        try:
            arr = json.loads(s)
            if isinstance(arr, list):
                return "\n".join(str(x) for x in arr)
        except Exception:
            pass
    return s


def _result_content(r: dict[str, Any], content_fields: tuple[str, ...]) -> str:
    """按该库的 content_fields 拼出「审查参照正文」；都为空时退回 display_text/embedding_text。"""
    parts = [_render_field(r[f]) for f in content_fields if r.get(f)]
    if parts:
        return "\n".join(p for p in parts if p)
    return (r.get("display_text") or r.get("embedding_text") or "").strip()


def _format_results(
    results: list[dict[str, Any]], content_fields: tuple[str, ...], *, body_limit: int = 800
) -> str:
    """把检索结果编号格式化成给总结 LLM 的上下文块。"""
    parts: list[str] = []
    for i, r in enumerate(results, start=1):
        label = _result_label(r)
        src = _result_source(r, label)
        body = _result_content(r, content_fields)
        meta_bits = []
        if r.get("contract_domains"):
            meta_bits.append("领域:" + "/".join(r["contract_domains"]))
        if r.get("clause_types"):
            meta_bits.append("类型:" + "/".join(r["clause_types"]))
        meta_line = ("　" + "　".join(meta_bits)) if meta_bits else ""
        case_cites = _case_cites_text(r)
        extra_lines: list[str] = []
        if r.get("relevant_statutes"):
            extra_lines.append(f"相关法条原文：{str(r.get('relevant_statutes')).strip()}")
        elif case_cites:
            extra_lines.append(f"相关法条：{case_cites}")
        extra_text = "\n".join(extra_lines)
        extra_block = f"{extra_text}\n" if extra_text else ""
        parts.append(
            f"[{i}] {label}（相似度 {r.get('score')}　来源 {src}）{meta_line}\n"
            f"{extra_block}{body[:body_limit]}"
        )
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# 总结
# ---------------------------------------------------------------------------

_SUMMARIZE_SYSTEM = """你是法律检索分析助手。下面给你「{display_name}」里检索到的若干材料，以及用户的检索意图
（可能还附带一条正在审查的合同条款）。{hint}

要求：
- 挑出与【检索意图】相关、有帮助的材料；若附带了【当前待审条款】，则优先挑对审查该条款有用的。无关的舍弃。
- 每条保留项用 1–2 句给出可用结论（它说明了什么、有什么用），并在句尾用「(来源: …)」标注其标题或链接。
- 若确实没有与检索意图相关的材料，只回一句：「未检索到相关内容。」
- 输出简洁中文，不超过 6 条；不要复述原文大段，不要编造材料里没有的内容。
- 注意：这些是**参照材料，不是法律条文**；不要把它们当作法条引用。"""

_JUDICIAL_SUMMARIZE_SYSTEM = """你是司法解释检索分析助手。下面给你「{display_name}」里检索到的若干司法解释条文，
以及用户的检索意图（可能还附带一条正在审查的合同条款）。{hint}

要求：
- 判断相关性以【检索意图】为准；若附带【当前待审条款】，再优先挑对该条款审查有用的条文。
- 材料只要列明检索问题的具体情形、构成要件、认定标准、法律后果或法院口径，就属于直接相关；不得因此说「未检索到」。
- 每条保留项写明司法解释名称/条号，并用 1–3 句概括其口径；遇到条文列举具体情形的，应完整列出要点，不要只写笼统结论。
- 若确实没有任何材料能回答检索意图，只回一句：「未检索到直接相关的司法解释口径。」
- 输出简洁中文，不超过 6 条；不要编造材料里没有的内容。
- 司法解释条文是参照口径；若最终回答要作为正式法律引用，仍需按系统要求核验法条。"""

_CASE_SUMMARIZE_SYSTEM = """你是法律类案检索分析助手。下面给你「{display_name}」里检索到的若干案例材料，以及用户的检索意图
（可能还附带一条正在审查的合同条款）。{hint}

要求：
- 只保留与【检索意图】直接相关的案例；若材料相关性弱，明确说弱在哪里。
- 每个保留案例用一个小段落总结，必须包含：案件名称/日期、相关法条（材料中有才写，必须完整列出，不要只挑其中几条）、裁判要旨、基本案情、裁判理由或裁判结果。
- 不要复制长篇原文，要用你自己的话压缩；每个案例控制在 4–6 行。
- 最多输出 5 个案例；若确实没有直接相关案例，只回一句：「未检索到直接相关的类案裁判规则。」
- 案例是**参照材料，不是法律条文**；不要把案例来源编号当作法条引用。"""


def _summarize_system_for(spec: KBSpec) -> str:
    if spec.key == "case":
        template = _CASE_SUMMARIZE_SYSTEM
    elif spec.key == "judicial":
        template = _JUDICIAL_SUMMARIZE_SYSTEM
    else:
        template = _SUMMARIZE_SYSTEM
    return template.format(display_name=spec.display_name, hint=spec.summarize_hint)


def _summarize_body_limit(spec: KBSpec) -> int:
    # case 需要让摘要模型看到案情/理由/结果，默认 800 字会截掉关键上下文。
    return 2200 if spec.key == "case" else 800


def _build_human_prompt(query: str, clause_ctx: str, results_block: str) -> str:
    clause_part = f"【当前待审条款】\n{clause_ctx}\n\n" if clause_ctx else ""
    return (
        f"{clause_part}"
        f"【检索意图】{query}\n\n"
        f"【检索到的材料】\n{results_block}\n\n"
        f"请按要求萃取对本条款审查有用的要点。"
    )


def _fallback_digest(spec: KBSpec, results: list[dict[str, Any]]) -> str:
    """总结模型不可用时的降级输出：直接给原始召回，保证审查 agent 仍拿到材料。"""
    head = f"（{spec.display_name}自动摘要暂不可用，以下为原始召回，请自行甄别相关性）"
    return head + "\n\n" + _format_results(results, spec.content_fields, body_limit=800 if spec.key == "case" else 400)


def _summarize(spec: KBSpec, query: str, clause_ctx: str, results: list[dict[str, Any]]) -> str:
    """用快模型把检索结果总结成对当前条款有用的要点。失败则降级为原始召回。"""
    system = _summarize_system_for(spec)
    human = _build_human_prompt(
        query,
        clause_ctx,
        _format_results(results, spec.content_fields, body_limit=_summarize_body_limit(spec)),
    )
    try:
        resp = _run_with_timeout(
            f"summarize-{spec.key}",
            settings.kb_summarize_timeout,
            lambda: _get_summarize_llm().invoke(
                [SystemMessage(content=system), HumanMessage(content=human)],
                config={"tags": [KB_SUMMARIZE_TAG], "run_name": f"kb_summarize:{spec.key}"},
            ),
        )
        text = _content_to_text(getattr(resp, "content", "")).strip()
        return text or _fallback_digest(spec, results)
    except KBToolTimeoutError:
        logger.warning("[%s] 检索结果总结超时，降级为原始召回", spec.key)
        return _fallback_digest(spec, results)
    except Exception:
        logger.exception("[%s] 检索结果总结失败，降级为原始召回", spec.key)
        return _fallback_digest(spec, results)


def _trim_for_artifact(
    results: list[dict[str, Any]], content_fields: tuple[str, ...]
) -> list[dict[str, Any]]:
    """裁剪检索结果用于 artifact（前端/追溯），避免把整段正文塞进消息。"""
    out = []
    for r in results:
        out.append(
            {
                "chunk_id": r.get("chunk_id", ""),
                "label": _result_label(r),
                "score": r.get("score"),
                "source_url": r.get("source_url", ""),
                "contract_domains": r.get("contract_domains", []),
                "clause_types": r.get("clause_types", []),
                "excerpt": _result_content(r, content_fields)[:500],
            }
        )
    return out


# ---------------------------------------------------------------------------
# 工具体 + 工厂
# ---------------------------------------------------------------------------

def _run_kb_search(
    spec: KBSpec,
    *,
    query: str,
    state: Any,
    contract_domain: str = "",
    clause_type: str = "",
) -> tuple[str, dict[str, Any]]:
    """单个知识库「检索 + 总结」工具的共享实现。"""
    query = (query or "").strip()
    if not query:
        return f"（请提供检索意图后再调用 {spec.tool_name}）", {"kb": spec.key, "results": []}

    started = time.perf_counter()
    try:
        results = _run_with_timeout(
            f"retrieve-{spec.key}",
            settings.kb_retrieve_timeout,
            lambda: KBRetriever(spec.collection).search(
                query, contract_domain=contract_domain, clause_type=clause_type
            ),
        )
    except KBToolTimeoutError as exc:
        logger.warning(
            "[%s] 支撑库检索超时 query=%r elapsed=%.0fms",
            spec.key, query, (time.perf_counter() - started) * 1000,
        )
        return (
            f"（{spec.display_name}检索超时：{exc}。请基于已有条款内容继续审查，必要时稍后重试该检索。）",
            {
                "kb": spec.key,
                "collection": spec.collection,
                "query": query,
                "error": "timeout",
                "results": [],
            },
        )
    except Exception as exc:
        logger.exception("[%s] 支撑库检索失败 query=%r", spec.key, query)
        return (
            f"（{spec.display_name}检索失败：{type(exc).__name__}: {exc}。请基于已有条款内容继续审查。）",
            {
                "kb": spec.key,
                "collection": spec.collection,
                "query": query,
                "error": type(exc).__name__,
                "results": [],
            },
        )
    logger.info(
        "[%s] 支撑库检索完成 query=%r hits=%s elapsed=%.0fms",
        spec.key, query, len(results), (time.perf_counter() - started) * 1000,
    )
    if not results:
        # 空召回直接短路，不调用总结模型（省成本）。
        return (
            f"（{spec.display_name}未检索到与「{query}」相关的内容。）",
            {"kb": spec.key, "collection": spec.collection, "query": query, "results": []},
        )

    clause_ctx = _extract_clause_context(state)
    digest = _summarize(spec, query, clause_ctx, results)
    artifact = {
        "kb": spec.key,
        "collection": spec.collection,
        "query": query,
        "results": _trim_for_artifact(results, spec.content_fields),
    }
    return digest, artifact


def build_kb_tool(spec: KBSpec):
    """据一条 KBSpec 构建对应的「检索 + 总结」LangChain 工具。"""

    def _kb_search(
        query: str,
        state: Annotated[dict, InjectedState],
        contract_domain: str = "",
        clause_type: str = "",
    ) -> tuple[str, dict[str, Any]]:
        return _run_kb_search(
            spec,
            query=query,
            state=state,
            contract_domain=contract_domain,
            clause_type=clause_type,
        )

    _kb_search.__name__ = spec.tool_name
    # 工具 description = 规格里的 tool_desc（指导审查 LLM 何时调、query 写什么）。
    _kb_search.__doc__ = spec.tool_desc + (
        "\n\n参数：query=检索意图（必填）；contract_domain=可选合同领域；clause_type=可选条款类型。"
    )
    return tool(spec.tool_name, response_format="content_and_artifact")(_kb_search)


def make_kb_tools() -> list:
    """构建全部 5 个知识库检索总结工具（供审查 agent / supervisor 挂载）。"""
    return [build_kb_tool(spec) for spec in KB_REGISTRY]


__all__ = ["build_kb_tool", "make_kb_tools"]
