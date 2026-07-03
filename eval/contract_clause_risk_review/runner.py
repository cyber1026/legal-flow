"""合同条款风险评测运行器。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from eval.contract_clause_risk_review.cache import JsonFileCache
from eval.contract_clause_risk_review.io_utils import read_jsonl, write_jsonl
from eval.contract_clause_risk_review.schemas import EvalSample, PredictionRecord, map_system_label

Reviewer = Callable[[EvalSample], Awaitable[dict[str, Any]]]


class ReviewRunner:
    """批量调用单条款审查入口并保存预测结果。"""

    def __init__(
        self,
        *,
        cache: JsonFileCache,
        reviewer: Reviewer | None = None,
        party_stance: str = "中立",
        concurrency: int = 2,
        force: bool = False,
    ) -> None:
        """初始化评测运行器。"""
        self.cache = cache
        self.reviewer = reviewer or (lambda sample: review_sample_with_system(sample, party_stance=party_stance))
        self.party_stance = party_stance
        self.concurrency = max(1, concurrency)
        self.force = force

    async def run_dataset(self, dataset_path: Path, predictions_path: Path) -> list[PredictionRecord]:
        """运行整个 JSONL 数据集，并支持按 sample_id 断点继续。"""
        samples = [EvalSample.from_dict(row) for row in read_jsonl(dataset_path)]
        existing = {
            row["sample_id"]: PredictionRecord(
                sample_id=row["sample_id"],
                gold_label=row["gold_label"],
                predicted_label=row.get("predicted_label"),
                predicted_system_level=row.get("predicted_system_level", ""),
                success=bool(row.get("success", True)),
                error=row.get("error", ""),
                elapsed_seconds=float(row.get("elapsed_seconds", 0.0)),
                has_opinion=bool(row.get("has_opinion", False)),
                opinion_count=int(row.get("opinion_count", 0)),
                verified_citation_count=int(row.get("verified_citation_count", 0)),
                citation_count=int(row.get("citation_count", 0)),
                raw_review=dict(row.get("raw_review") or {}),
            )
            for row in read_jsonl(predictions_path)
        }
        semaphore = asyncio.Semaphore(self.concurrency)

        async def run_one(sample: EvalSample) -> None:
            if sample.sample_id in existing and not self.force:
                return
            async with semaphore:
                existing[sample.sample_id] = await self._run_one(sample)

        await asyncio.gather(*(run_one(sample) for sample in samples))
        records = [existing[key] for key in sorted(existing)]
        write_jsonl(predictions_path, [record.to_dict() for record in records])
        return records

    async def _run_one(self, sample: EvalSample) -> PredictionRecord:
        """运行单个样本并转换为预测记录。"""
        started = time.perf_counter()
        cache_task = "review_prediction"
        cache_key = self.cache.build_key(
            task=cache_task,
            prompt_hash="system_review_entry",
            input_payload={"sample_id": sample.sample_id, "text": sample.text, "party_stance": self.party_stance},
            model="contract_review_agent",
        )
        if not self.force:
            cached = self.cache.get(cache_task, cache_key)
            if cached and isinstance(cached.get("output"), dict):
                return prediction_from_review(
                    sample,
                    cached["output"],
                    elapsed_seconds=float(cached["output"].get("elapsed_seconds", 0.0)),
                )

        try:
            review = await self.reviewer(sample)
            elapsed = round(time.perf_counter() - started, 3)
            review["elapsed_seconds"] = elapsed
            self.cache.set(
                cache_task,
                cache_key,
                model="contract_review_agent",
                prompt_hash="system_review_entry",
                input_payload={"sample_id": sample.sample_id, "text": sample.text, "party_stance": self.party_stance},
                output=review,
            )
            return prediction_from_review(sample, review, elapsed_seconds=elapsed)
        except Exception as exc:
            return PredictionRecord(
                sample_id=sample.sample_id,
                gold_label=sample.gold_label,
                predicted_label=None,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                elapsed_seconds=round(time.perf_counter() - started, 3),
            )


async def review_sample_with_system(sample: EvalSample, *, party_stance: str = "中立") -> dict[str, Any]:
    """调用现有单条款审查 Agent 审查一个评测样本。"""
    from eval.contract_clause_risk_review.schemas import EvalSample as _EvalSample

    if not isinstance(sample, _EvalSample):
        raise TypeError("sample 必须是 EvalSample")

    from app.contracts.review_agent import areview_clause_events

    final_review: Any = None
    async for event in areview_clause_events(
        contract_title=sample.contract_name,
        section_path=sample.section_path,
        clause_no=sample.clause_no,
        clause_text=sample.text,
        party_stance=party_stance,
        focus_dimensions=[],
        run_config={"run_name": f"eval_contract_clause_risk_review:{sample.sample_id}"},
    ):
        if event.get("type") == "result":
            final_review = event.get("review")
    if final_review is None:
        raise RuntimeError("审查 Agent 未返回 result 事件")
    return _review_to_dict(final_review)


def _review_to_dict(review: Any) -> dict[str, Any]:
    """把 ReviewOutput 或普通对象转成字典。"""
    if hasattr(review, "model_dump"):
        return review.model_dump()
    if isinstance(review, dict):
        return review
    raise TypeError(f"不支持的审查结果类型：{type(review)!r}")


def prediction_from_review(
    sample: EvalSample,
    review: dict[str, Any],
    *,
    elapsed_seconds: float,
) -> PredictionRecord:
    """把审查输出转换成评测预测记录。"""
    risk = dict(review.get("risk_assessment") or {})
    system_level = str(risk.get("risk_level", ""))
    predicted = map_system_label(system_level)
    opinions = list(review.get("opinions") or [])
    citation_count = 0
    verified_count = 0
    for opinion in opinions:
        citations = list((opinion or {}).get("citations") or [])
        citation_count += len(citations)
        verified_count += sum(1 for citation in citations if (citation or {}).get("verified"))
    return PredictionRecord(
        sample_id=sample.sample_id,
        gold_label=sample.gold_label,
        predicted_label=predicted,
        predicted_system_level=system_level,
        success=True,
        elapsed_seconds=elapsed_seconds,
        has_opinion=bool(review.get("has_opinion")),
        opinion_count=len(opinions),
        verified_citation_count=verified_count,
        citation_count=citation_count,
        raw_review=review,
    )


def _prediction_from_review(
    sample: EvalSample,
    review: dict[str, Any],
    *,
    elapsed_seconds: float,
) -> PredictionRecord:
    """兼容旧内部调用名，实际委托给公开转换函数。"""
    return prediction_from_review(sample, review, elapsed_seconds=elapsed_seconds)
