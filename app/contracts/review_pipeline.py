"""合同审查 Pipeline（流式编排门面）。

编排本体是一张显式的 LangGraph StateGraph（见 review_graph.py）。本模块只保留对外
入口 `astream_review_job`：它是 async 生成器，逐事件 yield ``{"event": str, "data": dict}``
供上层（review_manager → 审查 SSE 路由）转 SSE。

实现上，它驱动 `get_review_graph().astream(stream_mode="custom")`：图各节点用
`get_stream_writer()` 把进度事件推到 custom 流，本门面直接 re-yield，运行时自动做
多条款并行事件的 fan-in。并发上限经 `config={"max_concurrency": ...}` 控制。

状态机：pending → parsing → embedding → reviewing → done / failed。
事件类型：status / clause_start / clause_think_delta /
clause_tool_start / clause_tool_end / clause_done / report_ready /
consistency_start / consistency_delta / consistency_think_delta / consistency_done /
overview_start / overview_think_delta / overview_delta / overview_done / done / error。
（条款/一致性 agent 不再推 content/正文 增量：正式产出只走 submit_* 工具，content 通道是噪声。）
其中 report_ready 在条款级结果全部落库（aggregate）后立即发出，让前端预取条款级意见；
审查报告气泡需等一致性审查结束后再展示。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from app.contracts.review_graph import ReviewState, get_review_graph
from app.contracts.store import ContractStore
from app.core.config import settings
from app.core.observability import build_run_config, new_correlation_id

logger = logging.getLogger(__name__)


async def astream_review_job(contract_id: int) -> AsyncIterator[dict[str, Any]]:
    """端到端流式跑合同审查，逐事件 yield ``{"event": str, "data": dict}``。

    内部驱动一张 LangGraph StateGraph；签名与事件契约保持不变，故 review_manager /
    审查 SSE 路由 / SSE wire 协议均无需改动。
    """
    # 审查跑在 review_manager 的后台任务里（与 HTTP 请求解耦），故在此自起一个 corr_id：
    # 本次审查图内所有节点/嵌套 agent 的日志与 LangSmith trace 都共享它，可双向跳转。
    corr = new_correlation_id()

    contract = await asyncio.to_thread(ContractStore.get_by_id, contract_id)
    if contract is None:
        yield {"event": "error", "data": {"message": f"contract not found: {contract_id}"}}
        return

    logger.info(
        "合同审查开始 contract=%s title=%s corr=%s",
        contract_id, contract.title or contract.filename, corr,
    )

    initial: ReviewState = {
        "contract_id": contract_id,
        "contract_title": contract.title or contract.filename,
        "session_id": contract.session_id,
        "party_stance": "未知",
        "contract_clauses": [],
        "clause_categories": {},
        "findings": [],
        "failed_clauses": [],
        "consistency_review": {},
        "risk_count": 0,
        "final_report": {},
    }

    try:
        # 审查图是最外层 root run：corr_id 绑为 LangSmith 根 run_id，子节点/条款/总览自动挂到此 run 下。
        config = build_run_config(
            run_name=f"review:{contract_id}",
            tags=["contract_review"],
            metadata={
                "contract_id": contract_id,
                "session_id": contract.session_id,
                "llm_provider": settings.llm_provider,
            },
            root=True,
            max_concurrency=max(1, settings.review_concurrency),
        )
        async for event in get_review_graph().astream(
            initial,
            stream_mode="custom",
            config=config,
        ):
            yield event

    except asyncio.CancelledError:
        # 进程关闭等取消：标记 failed，避免留下死 reviewing 状态。
        logger.info("审查流被中断 contract=%s", contract_id)
        ContractStore.update_status(
            contract_id, status="failed", error="审查连接中断", finish=True
        )
        raise
    except Exception as exc:
        logger.exception("流式审查失败 contract=%s", contract_id)
        ContractStore.update_status(
            contract_id, status="failed", error=f"{type(exc).__name__}: {exc}", finish=True
        )
        yield {"event": "error", "data": {"message": f"{type(exc).__name__}: {exc}"}}


__all__ = ["astream_review_job"]
