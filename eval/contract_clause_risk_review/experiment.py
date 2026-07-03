"""条款风险审查 baseline/system 对照实验。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage
from pydantic import ValidationError
from tqdm.auto import tqdm

from app.contracts.prompts.risk_schema import ReviewOutput
from app.core.config import settings
from app.llm.factory import get_chat_llm
from eval.contract_clause_risk_review.cache import JsonFileCache
from eval.contract_clause_risk_review.dataset_builder import BuildConfig
from eval.contract_clause_risk_review.io_utils import ensure_dir, read_json, read_jsonl, stable_json_dumps, write_json, write_jsonl
from eval.contract_clause_risk_review.llm_client import parse_json_response
from eval.contract_clause_risk_review.metrics import compute_metrics, load_predictions
from eval.contract_clause_risk_review.prompts import prompt_hash
from eval.contract_clause_risk_review.runner import prediction_from_review
from eval.contract_clause_risk_review.schemas import EvalLabel, EvalSample, LABELS, PredictionRecord

SampleStrategy = Literal["auto", "all", "stratified"]
GroupName = Literal["baseline_llm", "system_agent"]

BASELINE_REVIEW_PROMPT = """你是一个不使用任何外部工具的合同条款法律风险审查模型。请只基于给定条款文本和一般法律知识，按中立立场审查条款风险。

风险等级必须使用系统五档：
- none：无风险或仅常规表述。
- low：低风险，通常只需提示或轻微完善。
- medium：中风险，存在明确不利或不完整安排，但通常可通过补充约定、谈判或提示控制。
- high：高风险，可能导致重大权利丧失、责任严重失衡、重大损失或关键救济缺失。
- critical：极高风险，可能明显违法无效、造成重大不可逆损失或严重合规后果。

输出必须是一个 JSON 对象，不要 Markdown，不要额外解释。字段如下：
{
  "has_opinion": true 或 false,
  "opinions": [
    {
      "opinion_type": "疑问/说明/提醒/建议/警告",
      "review_dimension": "主体合格性/内容合法性/条款实用性/权益明确性/合同严谨性/表述精确性",
      "finding": "审查发现",
      "recommendation": "处理建议",
      "confidence": 0.0 到 1.0,
      "citations": []
    }
  ],
  "risk_assessment": {
    "risk_level": "none/low/medium/high/critical",
    "rationale": "评级理由",
    "affected_party": "甲方/乙方/双方/不适用/未知",
    "confidence": 0.0 到 1.0
  },
  "consistency_facts": [],
  "note": "补充说明，无则空字符串",
  "reasoning_summary": "简要说明你如何判断风险，不要编造已检索依据"
}
"""


@dataclass(slots=True)
class ExperimentConfig:
    """一次条款风险对照实验的完整配置。"""

    dataset_path: Path = BuildConfig().dataset_path
    results_root: Path = Path("eval/contract_clause_risk_review/results")
    result_dir: Path | None = None
    run_name: str | None = None
    sample_strategy: SampleStrategy = "auto"
    limit: int | None = None
    seed: int | None = None
    party_stance: str = "中立"
    provider: str | None = None
    model: str | None = None
    temperature: float = 0.0
    baseline_concurrency: int = 4
    system_concurrency: int = 2
    cache_dir: Path = BuildConfig().cache_dir
    force: bool = False
    trace_max_tool_artifact_chars: int = 20000
    show_progress: bool = True

    def to_dict(self) -> dict[str, Any]:
        """转换成可落盘的 JSON 配置。"""
        data = asdict(self)
        for key in ("dataset_path", "results_root", "result_dir", "cache_dir"):
            if data.get(key) is not None:
                data[key] = str(data[key])
        data["run_mode"] = infer_run_mode(self.limit)
        data["effective_sample_strategy"] = resolve_sample_strategy(self.sample_strategy, self.limit)
        data["effective_seed"] = resolve_seed(self.seed)
        data["provider"] = self.provider or settings.llm_provider
        data["model"] = self.model or default_model_name(self.provider or settings.llm_provider)
        data["system_agent_uses_project_settings"] = True
        return data


@dataclass(slots=True)
class GroupReviewResult:
    """单个样本在一个实验组中的预测与 trace。"""

    prediction: PredictionRecord
    trace: dict[str, Any]


ReviewerFunc = Callable[[EvalSample], Awaitable[GroupReviewResult]]


class BaselineLLMReviewer:
    """纯 LLM API 对照组，不使用任何系统工具或检索库。"""

    def __init__(self, config: ExperimentConfig, cache: JsonFileCache) -> None:
        """初始化 baseline reviewer。"""
        self.config = config
        self.cache = cache
        self.provider = config.provider or settings.llm_provider
        self.model = config.model or default_model_name(self.provider)
        self.prompt_hash = prompt_hash(BASELINE_REVIEW_PROMPT)

    async def review(self, sample: EvalSample) -> GroupReviewResult:
        """审查单个样本并返回预测和 trace。"""
        started = time.perf_counter()
        input_payload = {
            "sample_id": sample.sample_id,
            "contract_name": sample.contract_name,
            "section_path": sample.section_path,
            "clause_no": sample.clause_no,
            "party_stance": self.config.party_stance,
            "clause_text": sample.text,
        }
        model_identity = self.model_identity()
        cache_key = self.cache.build_key(
            task="baseline_llm_review",
            prompt_hash=self.prompt_hash,
            input_payload=input_payload,
            model=model_identity,
        )
        if not self.config.force:
            cached = self.cache.get("baseline_llm_review", cache_key)
            if cached and isinstance(cached.get("output"), dict):
                return _group_result_from_cached(sample, cached["output"])

        prompt = _render_baseline_prompt(input_payload)
        llm = get_chat_llm(**self._llm_kwargs())
        attempts: list[dict[str, Any]] = []
        review: dict[str, Any] | None = None
        last_error = ""
        for attempt in range(1, 4):
            message = prompt
            if attempt > 1:
                message += (
                    "\n\n上一次响应无法解析或不符合 schema。请严格只输出一个 JSON 对象，"
                    "risk_assessment.risk_level 必须是 none/low/medium/high/critical。"
                )
            response = await llm.ainvoke([HumanMessage(content=message)])
            response_trace = _serialize_llm_response(response)
            try:
                parsed = parse_json_response(getattr(response, "content", ""))
                review = _normalize_baseline_review(parsed)
                attempts.append({"attempt": attempt, "success": True, "response": response_trace})
                break
            except (ValueError, ValidationError, json.JSONDecodeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                attempts.append({"attempt": attempt, "success": False, "error": last_error, "response": response_trace})
        if review is None:
            raise ValueError(f"baseline LLM 未产出合法审查 JSON：{last_error}")

        elapsed = round(time.perf_counter() - started, 3)
        output = {
            "sample_id": sample.sample_id,
            "group": "baseline_llm",
            "review": review,
            "trace": {
                "sample_id": sample.sample_id,
                "group": "baseline_llm",
                "prompt": prompt,
                "attempts": attempts,
                "model": model_identity,
                "tool_calls": [],
                "elapsed_seconds": elapsed,
            },
            "elapsed_seconds": elapsed,
        }
        self.cache.set(
            "baseline_llm_review",
            cache_key,
            model=model_identity,
            prompt_hash=self.prompt_hash,
            input_payload=input_payload,
            output=output,
        )
        return _group_result_from_cached(sample, output)

    def model_identity(self) -> str:
        """返回 baseline 缓存和报告使用的模型身份。"""
        return f"{self.provider}:{self.model}:temperature={self.config.temperature}:baseline_no_tools"

    def _llm_kwargs(self) -> dict[str, Any]:
        """构造 baseline LLM 调用参数。"""
        kwargs: dict[str, Any] = {
            "provider": self.provider,
            "model": self.model,
            "temperature": self.config.temperature,
            "timeout": settings.llm_review_timeout,
        }
        if self.provider.lower() == "deepseek":
            kwargs["enable_thinking"] = True
            kwargs["base_url"] = settings.deepseek_beta_base_url
        elif self.provider.lower() == "zhipuai":
            kwargs["enable_thinking"] = True
        return kwargs


class SystemAgentReviewer:
    """系统审查 agent 实验组，保留推理、工具调用和检索 artifact trace。"""

    def __init__(self, config: ExperimentConfig, cache: JsonFileCache) -> None:
        """初始化 system reviewer。"""
        self.config = config
        self.cache = cache

    async def review(self, sample: EvalSample) -> GroupReviewResult:
        """调用现有系统 agent 审查单个样本。"""
        started = time.perf_counter()
        input_payload = {
            "sample_id": sample.sample_id,
            "contract_name": sample.contract_name,
            "section_path": sample.section_path,
            "clause_no": sample.clause_no,
            "party_stance": self.config.party_stance,
            "clause_text": sample.text,
        }
        model_identity = system_model_identity()
        cache_key = self.cache.build_key(
            task="system_agent_review",
            prompt_hash="system_agent_areview_clause_events_v2",
            input_payload=input_payload,
            model=model_identity,
        )
        if not self.config.force:
            cached = self.cache.get("system_agent_review", cache_key)
            if cached and isinstance(cached.get("output"), dict):
                return _group_result_from_cached(sample, cached["output"])

        from app.contracts.review_agent import areview_clause_events

        thinking_parts: list[str] = []
        raw_events: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        tool_call_index: dict[str, dict[str, Any]] = {}
        review: dict[str, Any] | None = None

        async for event in areview_clause_events(
            contract_title=sample.contract_name,
            section_path=sample.section_path,
            clause_no=sample.clause_no,
            clause_text=sample.text,
            party_stance=self.config.party_stance,
            focus_dimensions=[],
            run_config={"run_name": f"eval_contract_clause_risk_review:{sample.sample_id}"},
            include_tool_artifact=True,
            max_tool_artifact_chars=self.config.trace_max_tool_artifact_chars,
        ):
            serialized = _serialize_event(event)
            raw_events.append(serialized)
            event_type = serialized.get("type")
            if event_type == "think":
                thinking_parts.append(str(serialized.get("delta", "")))
            elif event_type == "tool_start":
                call = {
                    "name": serialized.get("name", ""),
                    "call_id": serialized.get("call_id", ""),
                    "args": serialized.get("args", {}),
                    "started_at_offset_seconds": round(time.perf_counter() - started, 3),
                }
                tool_calls.append(call)
                if call["call_id"]:
                    tool_call_index[str(call["call_id"])] = call
            elif event_type == "tool_end":
                call_id = str(serialized.get("call_id", ""))
                call = tool_call_index.get(call_id)
                if call is None:
                    call = {"name": serialized.get("name", ""), "call_id": call_id, "args": {}}
                    tool_calls.append(call)
                call["ended_at_offset_seconds"] = round(time.perf_counter() - started, 3)
                call["result_preview"] = serialized.get("result_preview", "")
                call["citations"] = serialized.get("citations", [])
                call["artifact"] = serialized.get("artifact")
            elif event_type == "result":
                review = _review_to_plain_dict(serialized.get("review"))

        if review is None:
            raise RuntimeError("system agent 未返回 result 事件")

        elapsed = round(time.perf_counter() - started, 3)
        trace = {
            "sample_id": sample.sample_id,
            "group": "system_agent",
            "model": model_identity,
            "visible_thinking": "".join(thinking_parts),
            "tool_calls": tool_calls,
            "raw_events": raw_events,
            "review": review,
            "elapsed_seconds": elapsed,
        }
        output = {
            "sample_id": sample.sample_id,
            "group": "system_agent",
            "review": review,
            "trace": trace,
            "elapsed_seconds": elapsed,
        }
        self.cache.set(
            "system_agent_review",
            cache_key,
            model=model_identity,
            prompt_hash="system_agent_areview_clause_events_v2",
            input_payload=input_payload,
            output=output,
        )
        return _group_result_from_cached(sample, output)


async def run_experiment(
    config: ExperimentConfig,
    *,
    baseline_reviewer: ReviewerFunc | None = None,
    system_reviewer: ReviewerFunc | None = None,
) -> dict[str, Any]:
    """执行完整 baseline/system 对照实验并保存所有产物。"""
    started_wall = datetime.now().astimezone()
    started_perf = time.perf_counter()
    effective_strategy = resolve_sample_strategy(config.sample_strategy, config.limit)
    effective_seed = resolve_seed(config.seed)
    result_dir = config.result_dir or create_result_dir(config.results_root, config.run_name)
    ensure_dir(result_dir)
    run_config = config.to_dict()
    run_config["result_dir"] = str(result_dir)
    run_config["started_at"] = started_wall.isoformat()
    write_json(result_dir / "run_config.json", run_config)

    all_samples = load_eval_samples(config.dataset_path)
    samples = select_samples(all_samples, strategy=effective_strategy, limit=config.limit, seed=effective_seed)
    sample_by_id = {sample.sample_id: sample for sample in samples}
    snapshot = build_dataset_snapshot(config.dataset_path, samples)
    write_json(result_dir / "dataset_snapshot.json", snapshot)

    cache = JsonFileCache(config.cache_dir)
    baseline = baseline_reviewer or BaselineLLMReviewer(config, cache).review
    system = system_reviewer or SystemAgentReviewer(config, cache).review
    baseline_records = await run_group(
        group="baseline_llm",
        samples=samples,
        result_dir=result_dir,
        reviewer=baseline,
        concurrency=config.baseline_concurrency,
        force=config.force,
        show_progress=config.show_progress,
    )
    system_records = await run_group(
        group="system_agent",
        samples=samples,
        result_dir=result_dir,
        reviewer=system,
        concurrency=config.system_concurrency,
        force=config.force,
        show_progress=config.show_progress,
    )

    trace_stats = {
        "baseline_llm": collect_trace_stats(result_dir / "groups" / "baseline_llm" / "traces"),
        "system_agent": collect_trace_stats(result_dir / "groups" / "system_agent" / "traces"),
    }
    metrics_summary = build_metrics_summary(
        {"baseline_llm": baseline_records, "system_agent": system_records},
        trace_stats=trace_stats,
    )
    confusion_matrices = {
        "baseline_llm": compute_metrics(baseline_records)["confusion_matrix"],
        "system_agent": compute_metrics(system_records)["confusion_matrix"],
    }
    paired = build_paired_comparison(samples, baseline_records, system_records)
    write_json(result_dir / "metrics_summary.json", metrics_summary)
    write_json(result_dir / "confusion_matrices.json", confusion_matrices)
    write_json(result_dir / "paired_comparison.json", paired)
    write_experiment_report(
        result_dir / "experiment_report.md",
        run_config=run_config,
        snapshot=snapshot,
        metrics_summary=metrics_summary,
        confusion_matrices=confusion_matrices,
        paired=paired,
        sample_by_id=sample_by_id,
    )
    write_case_reports(result_dir / "case_reports", samples, baseline_records, system_records, result_dir)

    elapsed = round(time.perf_counter() - started_perf, 3)
    manifest = {
        "started_at": started_wall.isoformat(),
        "finished_at": datetime.now().astimezone().isoformat(),
        "elapsed_seconds": elapsed,
        "dataset": str(config.dataset_path),
        "result_dir": str(result_dir),
        "groups": {
            "baseline_llm": _group_run_counts(baseline_records),
            "system_agent": _group_run_counts(system_records),
        },
        "cache": cache.stats(),
        "outputs": list_output_files(result_dir),
    }
    write_json(result_dir / "run_manifest.json", manifest)
    return manifest


async def run_group(
    *,
    group: GroupName,
    samples: list[EvalSample],
    result_dir: Path,
    reviewer: ReviewerFunc,
    concurrency: int,
    force: bool,
    show_progress: bool = True,
) -> list[PredictionRecord]:
    """运行单个实验组，逐样本保存 predictions 和 trace。"""
    group_dir = ensure_dir(result_dir / "groups" / group)
    traces_dir = ensure_dir(group_dir / "traces")
    predictions_path = group_dir / "predictions.jsonl"
    existing = {
        record.sample_id: record
        for record in load_predictions(predictions_path)
    }
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run_one(sample: EvalSample) -> None:
        trace_path = traces_dir / f"{safe_file_stem(sample.sample_id)}.json"
        if sample.sample_id in existing and trace_path.exists() and not force:
            return
        async with semaphore:
            started = time.perf_counter()
            try:
                result = await reviewer(sample)
            except Exception as exc:
                elapsed = round(time.perf_counter() - started, 3)
                result = GroupReviewResult(
                    prediction=PredictionRecord(
                        sample_id=sample.sample_id,
                        gold_label=sample.gold_label,
                        predicted_label=None,
                        success=False,
                        error=f"{type(exc).__name__}: {exc}",
                        elapsed_seconds=elapsed,
                    ),
                    trace={
                        "sample_id": sample.sample_id,
                        "group": group,
                        "success": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "elapsed_seconds": elapsed,
                    },
                )
            async with lock:
                existing[sample.sample_id] = result.prediction
                write_json(trace_path, result.trace)
                ordered = [existing[sample.sample_id] for sample in samples if sample.sample_id in existing]
                write_jsonl(predictions_path, [record.to_dict() for record in ordered])

    tasks = [asyncio.create_task(run_one(sample)) for sample in samples]
    with tqdm(
        total=len(tasks),
        desc=group,
        unit="sample",
        disable=not show_progress,
    ) as progress:
        for task in asyncio.as_completed(tasks):
            await task
            progress.update(1)
    return [existing[sample.sample_id] for sample in samples if sample.sample_id in existing]


def create_result_dir(root: Path, run_name: str | None = None, *, timestamp: str | None = None) -> Path:
    """按时间戳和可选 run name 创建唯一结果目录。"""
    ensure_dir(root)
    stamp = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    clean_name = sanitize_run_name(run_name or "")
    dirname = f"{stamp}_{clean_name}" if clean_name else stamp
    candidate = root / dirname
    if not candidate.exists():
        return ensure_dir(candidate)
    while True:
        suffix = uuid.uuid4().hex[:6]
        candidate = root / f"{dirname}_{suffix}"
        if not candidate.exists():
            return ensure_dir(candidate)


def sanitize_run_name(run_name: str) -> str:
    """把 run name 清洗为安全目录名。"""
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", (run_name or "").strip())
    cleaned = cleaned.strip("-._")
    return cleaned


def infer_run_mode(limit: int | None) -> str:
    """按是否限制样本数推断本次实验是 smoke 还是 full。"""
    return "smoke" if limit is not None else "full"


def resolve_sample_strategy(strategy: SampleStrategy, limit: int | None) -> Literal["all", "stratified"]:
    """解析抽样策略；auto 在有限量时分层，全量时使用全部样本。"""
    if strategy == "auto":
        return "stratified" if limit is not None else "all"
    return strategy


def resolve_seed(seed: int | None) -> int:
    """解析随机种子；未指定时使用当天日期 YYYYMMDD。"""
    return seed if seed is not None else int(datetime.now().astimezone().strftime("%Y%m%d"))


def load_eval_samples(path: Path) -> list[EvalSample]:
    """读取评测集样本。"""
    return [EvalSample.from_dict(row) for row in read_jsonl(path)]


def select_samples(
    samples: list[EvalSample],
    *,
    strategy: SampleStrategy,
    limit: int | None,
    seed: int,
) -> list[EvalSample]:
    """按 all 或分层策略选择实验样本。"""
    if strategy == "all":
        return samples if limit is None else samples[:limit]
    if strategy != "stratified":
        raise ValueError(f"未知抽样策略：{strategy}")
    if limit is None:
        limit = len(samples)
    rng = random.Random(seed)
    by_label: dict[EvalLabel, list[EvalSample]] = {label: [] for label in LABELS}
    for sample in samples:
        by_label[sample.gold_label].append(sample)
    for items in by_label.values():
        rng.shuffle(items)
    base = limit // len(LABELS)
    remainder = limit % len(LABELS)
    selected: list[EvalSample] = []
    for index, label in enumerate(LABELS):
        take = base + (1 if index < remainder else 0)
        selected.extend(by_label[label][:take])
    rng.shuffle(selected)
    return selected


def build_dataset_snapshot(dataset_path: Path, samples: list[EvalSample]) -> dict[str, Any]:
    """构造本次实验使用的数据集快照信息。"""
    manifest_path = dataset_path.parent / "contract_risk_manifest.json"
    manifest = read_json(manifest_path, default={}) or {}
    label_counts = Counter(sample.gold_label for sample in samples)
    return {
        "dataset_path": str(dataset_path),
        "dataset_sha256": file_sha256(dataset_path),
        "sample_count": len(samples),
        "label_distribution": {label: label_counts.get(label, 0) for label in LABELS},
        "sample_ids": [sample.sample_id for sample in samples],
        "manifest_path": str(manifest_path) if manifest_path.exists() else "",
        "manifest_summary": {
            "counts": manifest.get("counts", {}),
            "selection_gaps": manifest.get("selection_gaps", {}),
            "prompt_hashes": manifest.get("prompt_hashes", {}),
            "risk_label_scale": manifest.get("risk_label_scale", []),
        },
    }


def build_metrics_summary(
    records_by_group: dict[GroupName, list[PredictionRecord]],
    *,
    trace_stats: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """生成实验报告使用的核心指标摘要。"""
    summary: dict[str, Any] = {}
    for group, records in records_by_group.items():
        raw = compute_metrics(records)
        risk_binary = raw["risk_binary"]
        engineering = raw["engineering"]
        summary[group] = {
            "total": raw["total"],
            "valid": raw["valid"],
            "failed": raw["failed"],
            "accuracy": risk_binary["accuracy"],
            "risk_precision": risk_binary["precision"],
            "risk_recall": risk_binary["recall"],
            "risk_f1": risk_binary["f1"],
            "high_risk_recall": raw["high_recall"],
            "risk_level_accuracy": raw["accuracy"],
            "failure_rate": engineering["failure_rate"],
            "avg_latency": engineering["avg_elapsed_seconds"],
            "avg_opinion_count": engineering["avg_opinion_count"],
            "verified_citation_ratio": engineering["verified_citation_ratio"],
            "tool_stats": trace_stats.get(group, {}),
            "raw_metrics": raw,
        }
    summary["delta_system_minus_baseline"] = {
        key: round(summary["system_agent"][key] - summary["baseline_llm"][key], 6)
        for key in (
            "accuracy",
            "risk_precision",
            "risk_recall",
            "risk_f1",
            "high_risk_recall",
            "risk_level_accuracy",
            "failure_rate",
            "avg_latency",
        )
    }
    return summary


def build_paired_comparison(
    samples: list[EvalSample],
    baseline_records: list[PredictionRecord],
    system_records: list[PredictionRecord],
) -> dict[str, Any]:
    """构造 baseline 与 system 的逐样本成对比较。"""
    baseline = {record.sample_id: record for record in baseline_records}
    system = {record.sample_id: record for record in system_records}
    rows: list[dict[str, Any]] = []
    improved: list[dict[str, Any]] = []
    regressed: list[dict[str, Any]] = []
    high_misses: list[dict[str, Any]] = []
    safe_false_positives: list[dict[str, Any]] = []
    for sample in samples:
        b = baseline.get(sample.sample_id)
        s = system.get(sample.sample_id)
        row = {
            "sample_id": sample.sample_id,
            "gold_label": sample.gold_label,
            "baseline_predicted_label": b.predicted_label if b else None,
            "system_predicted_label": s.predicted_label if s else None,
            "baseline_success": bool(b and b.success),
            "system_success": bool(s and s.success),
            "baseline_exact_correct": bool(b and b.success and b.predicted_label == sample.gold_label),
            "system_exact_correct": bool(s and s.success and s.predicted_label == sample.gold_label),
            "baseline_risk_correct": _risk_binary_correct(sample.gold_label, b.predicted_label if b else None),
            "system_risk_correct": _risk_binary_correct(sample.gold_label, s.predicted_label if s else None),
        }
        if row["system_exact_correct"] and not row["baseline_exact_correct"]:
            row["category"] = "improved"
            improved.append(row)
        elif row["baseline_exact_correct"] and not row["system_exact_correct"]:
            row["category"] = "regressed"
            regressed.append(row)
        elif row["system_exact_correct"] and row["baseline_exact_correct"]:
            row["category"] = "same_correct"
        else:
            row["category"] = "same_wrong"
        if sample.gold_label == "high" and (s is None or s.predicted_label != "high"):
            high_misses.append(row)
        if sample.gold_label == "safe" and s is not None and s.predicted_label != "safe":
            safe_false_positives.append(row)
        rows.append(row)
    return {
        "counts": Counter(row["category"] for row in rows),
        "rows": rows,
        "improved": improved,
        "regressed": regressed,
        "system_high_misses": high_misses,
        "system_safe_false_positives": safe_false_positives,
    }


def collect_trace_stats(traces_dir: Path) -> dict[str, Any]:
    """从 trace 文件统计工具调用和检索规模。"""
    if not traces_dir.exists():
        return {"trace_count": 0, "tool_call_count": 0, "avg_tool_calls": 0.0, "tool_call_count_by_name": {}}
    paths = sorted(traces_dir.glob("*.json"))
    by_name: Counter[str] = Counter()
    total_tool_calls = 0
    total_citations = 0
    for path in paths:
        trace = read_json(path, default={}) or {}
        calls = list(trace.get("tool_calls") or [])
        total_tool_calls += len(calls)
        for call in calls:
            by_name[str(call.get("name", ""))] += 1
            total_citations += len(call.get("citations") or [])
    return {
        "trace_count": len(paths),
        "tool_call_count": total_tool_calls,
        "avg_tool_calls": _round_div(total_tool_calls, len(paths)),
        "citation_count_from_tools": total_citations,
        "avg_tool_citations": _round_div(total_citations, len(paths)),
        "tool_call_count_by_name": dict(sorted(by_name.items())),
    }


def write_experiment_report(
    path: Path,
    *,
    run_config: dict[str, Any],
    snapshot: dict[str, Any],
    metrics_summary: dict[str, Any],
    confusion_matrices: dict[str, Any],
    paired: dict[str, Any],
    sample_by_id: dict[str, EvalSample],
) -> None:
    """写入可读 Markdown 实验报告。"""
    lines: list[str] = []
    lines.append("# 条款风险审查对照实验报告")
    lines.append("")
    lines.append("## Run")
    lines.append("")
    lines.extend(_config_table(run_config))
    lines.append("")
    lines.append("## Dataset")
    lines.append("")
    lines.extend(_dataset_table(snapshot))
    lines.append("")
    lines.append("## Metrics")
    lines.append("")
    lines.extend(_metrics_table(metrics_summary))
    lines.append("")
    lines.append("## Delta")
    lines.append("")
    lines.extend(_delta_table(metrics_summary.get("delta_system_minus_baseline", {})))
    lines.append("")
    lines.append("## Confusion Matrices")
    lines.append("")
    for group, matrix in confusion_matrices.items():
        lines.append(f"### {group}")
        lines.append("")
        lines.extend(_confusion_table(matrix))
        lines.append("")
    lines.append("## System 改善样本")
    lines.append("")
    lines.extend(_paired_rows_table(paired.get("improved", []), sample_by_id, limit=30))
    lines.append("")
    lines.append("## System 退化样本")
    lines.append("")
    lines.extend(_paired_rows_table(paired.get("regressed", []), sample_by_id, limit=30))
    lines.append("")
    lines.append("## System 高风险漏检样本")
    lines.append("")
    lines.extend(_paired_rows_table(paired.get("system_high_misses", []), sample_by_id, limit=50))
    lines.append("")
    lines.append("## System Safe 误报样本")
    lines.append("")
    lines.extend(_paired_rows_table(paired.get("system_safe_false_positives", []), sample_by_id, limit=50))
    lines.append("")
    ensure_dir(path.parent)
    path.write_text("\n".join(lines), encoding="utf-8")


def write_case_reports(
    case_dir: Path,
    samples: list[EvalSample],
    baseline_records: list[PredictionRecord],
    system_records: list[PredictionRecord],
    result_dir: Path,
) -> None:
    """为每个样本写入单独 Markdown case report。"""
    ensure_dir(case_dir)
    baseline = {record.sample_id: record for record in baseline_records}
    system = {record.sample_id: record for record in system_records}
    for sample in samples:
        baseline_trace = read_json(
            result_dir / "groups" / "baseline_llm" / "traces" / f"{safe_file_stem(sample.sample_id)}.json",
            default={},
        ) or {}
        system_trace = read_json(
            result_dir / "groups" / "system_agent" / "traces" / f"{safe_file_stem(sample.sample_id)}.json",
            default={},
        ) or {}
        path = case_dir / f"{safe_file_stem(sample.sample_id)}.md"
        path.write_text(
            _render_case_report(sample, baseline.get(sample.sample_id), system.get(sample.sample_id), baseline_trace, system_trace),
            encoding="utf-8",
        )


def file_sha256(path: Path) -> str:
    """计算文件 SHA256。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def list_output_files(root: Path) -> list[str]:
    """列出结果目录下所有输出文件。"""
    return [str(path.relative_to(root)) for path in sorted(root.rglob("*")) if path.is_file()]


def default_model_name(provider: str) -> str:
    """读取指定 provider 的默认模型名。"""
    provider = provider.lower()
    if provider == "deepseek":
        return settings.deepseek_model
    if provider == "gemini":
        return settings.google_model
    if provider == "zhipuai":
        return settings.zhipuai_model
    return "default"


def system_model_identity() -> str:
    """返回系统审查 agent 当前配置的模型身份。"""
    provider = settings.llm_provider
    return f"{provider}:{default_model_name(provider)}:contract_review_agent"


def safe_file_stem(value: str) -> str:
    """把 sample_id 转换成安全文件名主体。"""
    return re.sub(r"[^0-9A-Za-z._-]+", "_", value)


def _render_baseline_prompt(input_payload: dict[str, Any]) -> str:
    """渲染 baseline 单样本 prompt。"""
    return f"{BASELINE_REVIEW_PROMPT}\n\n输入：\n{stable_json_dumps(input_payload)}"


def _normalize_baseline_review(parsed: dict[str, Any]) -> dict[str, Any]:
    """校验并规范 baseline 的 ReviewOutput 结构。"""
    payload = parsed.get("review_output") if isinstance(parsed.get("review_output"), dict) else parsed
    payload = parsed.get("review") if isinstance(parsed.get("review"), dict) else payload
    review = ReviewOutput.model_validate(payload).model_dump()
    if "reasoning_summary" in parsed:
        review["reasoning_summary"] = str(parsed.get("reasoning_summary") or "")
    return review


def _serialize_llm_response(response: Any) -> dict[str, Any]:
    """把 LLM response 转换成 trace 友好的字典。"""
    content = getattr(response, "content", "")
    additional = getattr(response, "additional_kwargs", {}) or {}
    return {
        "content": _jsonable(content),
        "content_text": _content_to_text(content),
        "reasoning_content": additional.get("reasoning_content", ""),
        "additional_kwargs": _jsonable(additional),
        "response_metadata": _jsonable(getattr(response, "response_metadata", {}) or {}),
    }


def _serialize_event(event: dict[str, Any]) -> dict[str, Any]:
    """把系统 agent 事件转换成可 JSON 落盘的字典。"""
    return _jsonable(event)


def _jsonable(value: Any) -> Any:
    """递归转换不可 JSON 序列化对象。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump())
    return str(value)


def _content_to_text(content: Any) -> str:
    """把多形态 LLM content 压成可读文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("thinking") or ""))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content or "")


def _review_to_plain_dict(review: Any) -> dict[str, Any]:
    """把 ReviewOutput 或字典转成普通 dict。"""
    if isinstance(review, dict):
        return _jsonable(review)
    if hasattr(review, "model_dump"):
        return review.model_dump()
    raise TypeError(f"不支持的 review 类型：{type(review)!r}")


def _group_result_from_cached(sample: EvalSample, output: dict[str, Any]) -> GroupReviewResult:
    """把缓存输出转换成 GroupReviewResult。"""
    review = dict(output.get("review") or {})
    elapsed = float(output.get("elapsed_seconds", 0.0))
    prediction = prediction_from_review(sample, review, elapsed_seconds=elapsed)
    trace = dict(output.get("trace") or {})
    trace.setdefault("sample_id", sample.sample_id)
    trace.setdefault("review", review)
    trace.setdefault("elapsed_seconds", elapsed)
    return GroupReviewResult(prediction=prediction, trace=trace)


def _group_run_counts(records: list[PredictionRecord]) -> dict[str, Any]:
    """统计一个实验组的成功和失败样本数。"""
    success = sum(1 for record in records if record.success)
    return {"total": len(records), "success": success, "failed": len(records) - success}


def _risk_binary_correct(gold: EvalLabel, predicted: EvalLabel | None) -> bool:
    """判断 safe/risk 二分类是否正确。"""
    if predicted is None:
        return False
    return (gold == "safe") == (predicted == "safe")


def _round_div(a: float, b: float) -> float:
    """安全除法并保留六位小数。"""
    return 0.0 if b == 0 else round(a / b, 6)


def _config_table(config: dict[str, Any]) -> list[str]:
    """把 run_config 渲染成 Markdown 表格。"""
    keys = [
        "run_name",
        "run_mode",
        "sample_strategy",
        "effective_sample_strategy",
        "limit",
        "seed",
        "effective_seed",
        "party_stance",
        "provider",
        "model",
        "temperature",
        "baseline_concurrency",
        "system_concurrency",
        "force",
    ]
    rows = ["| 参数 | 值 |", "| --- | --- |"]
    for key in keys:
        rows.append(f"| `{key}` | {_md(config.get(key, ''))} |")
    return rows


def _dataset_table(snapshot: dict[str, Any]) -> list[str]:
    """把数据集快照渲染成 Markdown 表格。"""
    distribution = snapshot.get("label_distribution", {})
    rows = ["| 字段 | 值 |", "| --- | --- |"]
    rows.append(f"| dataset | {_md(snapshot.get('dataset_path', ''))} |")
    rows.append(f"| sha256 | `{snapshot.get('dataset_sha256', '')}` |")
    rows.append(f"| sample_count | {snapshot.get('sample_count', 0)} |")
    rows.append(f"| label_distribution | {_md(stable_json_dumps(distribution))} |")
    return rows


def _metrics_table(summary: dict[str, Any]) -> list[str]:
    """把核心指标渲染成 Markdown 表格。"""
    headers = [
        "Group",
        "Accuracy",
        "Risk Precision",
        "Risk Recall",
        "Risk F1",
        "High Risk Recall",
        "Risk Level Accuracy",
        "Failure Rate",
        "Avg Latency",
        "Avg Tool Calls",
    ]
    rows = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for group in ("baseline_llm", "system_agent"):
        item = summary.get(group, {})
        tool_stats = item.get("tool_stats", {}) or {}
        rows.append(
            "| "
            + " | ".join(
                [
                    group,
                    _fmt(item.get("accuracy", 0.0)),
                    _fmt(item.get("risk_precision", 0.0)),
                    _fmt(item.get("risk_recall", 0.0)),
                    _fmt(item.get("risk_f1", 0.0)),
                    _fmt(item.get("high_risk_recall", 0.0)),
                    _fmt(item.get("risk_level_accuracy", 0.0)),
                    _fmt(item.get("failure_rate", 0.0)),
                    _fmt(item.get("avg_latency", 0.0)),
                    _fmt(tool_stats.get("avg_tool_calls", 0.0)),
                ]
            )
            + " |"
        )
    return rows


def _delta_table(delta: dict[str, Any]) -> list[str]:
    """把 system-baseline 指标差值渲染成 Markdown 表格。"""
    rows = ["| 指标 | system - baseline |", "| --- | --- |"]
    for key, value in delta.items():
        rows.append(f"| `{key}` | {_fmt(value)} |")
    return rows


def _confusion_table(matrix: dict[str, dict[str, int]]) -> list[str]:
    """把三分类混淆矩阵渲染成 Markdown 表格。"""
    rows = ["| Gold \\ Pred | safe | medium | high |", "| --- | ---: | ---: | ---: |"]
    for gold in LABELS:
        row = matrix.get(gold, {})
        rows.append(f"| {gold} | {row.get('safe', 0)} | {row.get('medium', 0)} | {row.get('high', 0)} |")
    return rows


def _paired_rows_table(rows_data: list[dict[str, Any]], sample_by_id: dict[str, EvalSample], *, limit: int) -> list[str]:
    """把改善/退化/漏检样本渲染成 Markdown 表格。"""
    if not rows_data:
        return ["无。"]
    rows = ["| sample_id | gold | baseline | system | 条款摘录 |", "| --- | --- | --- | --- | --- |"]
    for row in rows_data[:limit]:
        sample = sample_by_id.get(str(row.get("sample_id", "")))
        excerpt = _truncate(sample.text if sample else "", 120)
        rows.append(
            f"| `{row.get('sample_id', '')}` | {row.get('gold_label', '')} | "
            f"{row.get('baseline_predicted_label', '')} | {row.get('system_predicted_label', '')} | {_md(excerpt)} |"
        )
    if len(rows_data) > limit:
        rows.append(f"| ... | ... | ... | ... | 另有 {len(rows_data) - limit} 条未展示 |")
    return rows


def _render_case_report(
    sample: EvalSample,
    baseline: PredictionRecord | None,
    system: PredictionRecord | None,
    baseline_trace: dict[str, Any],
    system_trace: dict[str, Any],
) -> str:
    """渲染单样本 case report。"""
    lines = [
        f"# Case `{sample.sample_id}`",
        "",
        f"- Gold: `{sample.gold_label}`",
        f"- Contract: {sample.contract_name}",
        f"- Clause: {sample.clause_no or '-'}",
        f"- Section: {sample.section_path or '-'}",
        "",
        "## 条款",
        "",
        "```text",
        sample.text,
        "```",
        "",
        "## Baseline LLM",
        "",
    ]
    lines.extend(_record_summary_lines(baseline))
    lines.append("")
    lines.append("### Baseline Reasoning")
    lines.append("")
    lines.append(_md(_truncate(_baseline_reasoning_text(baseline, baseline_trace), 1500)))
    lines.append("")
    lines.append("## System Agent")
    lines.append("")
    lines.extend(_record_summary_lines(system))
    lines.append("")
    lines.append("### Visible Thinking")
    lines.append("")
    lines.append(_md(_truncate(str(system_trace.get("visible_thinking", "")), 1500)))
    lines.append("")
    lines.append("### Tool Calls")
    lines.append("")
    lines.extend(_tool_calls_table(system_trace.get("tool_calls", [])))
    lines.append("")
    lines.append("### Retrieved / Tool Artifact Excerpts")
    lines.append("")
    lines.extend(_tool_artifact_excerpt_lines(system_trace.get("tool_calls", [])))
    lines.append("")
    return "\n".join(lines)


def _record_summary_lines(record: PredictionRecord | None) -> list[str]:
    """渲染单条预测记录摘要。"""
    if record is None:
        return ["无预测记录。"]
    risk = dict(record.raw_review.get("risk_assessment") or {})
    return [
        f"- Success: `{record.success}`",
        f"- Predicted Label: `{record.predicted_label}`",
        f"- System Level: `{record.predicted_system_level}`",
        f"- Opinion Count: `{record.opinion_count}`",
        f"- Latency: `{record.elapsed_seconds}` seconds",
        f"- Rationale: {_md(_truncate(str(risk.get('rationale', '')), 500))}",
        f"- Error: {_md(record.error)}",
    ]


def _baseline_reasoning_text(record: PredictionRecord | None, trace: dict[str, Any]) -> str:
    """提取 baseline 的推理摘要。"""
    if record is None:
        return ""
    review_summary = str(record.raw_review.get("reasoning_summary", "") or "")
    attempts = list(trace.get("attempts") or [])
    reasoning = ""
    for attempt in reversed(attempts):
        response = dict(attempt.get("response") or {})
        reasoning = str(response.get("reasoning_content", "") or "")
        if reasoning:
            break
    return "\n\n".join(part for part in (review_summary, reasoning) if part)


def _tool_calls_table(tool_calls: Any) -> list[str]:
    """渲染工具调用表格。"""
    calls = list(tool_calls or [])
    if not calls:
        return ["无工具调用。"]
    rows = ["| 工具 | 参数摘要 | 引用数 | 结果摘要 |", "| --- | --- | ---: | --- |"]
    for call in calls[:20]:
        rows.append(
            f"| {_md(call.get('name', ''))} | {_md(_truncate(stable_json_dumps(call.get('args', {})), 120))} | "
            f"{len(call.get('citations') or [])} | {_md(_truncate(str(call.get('result_preview', '')), 160))} |"
        )
    if len(calls) > 20:
        rows.append(f"| ... | ... | ... | 另有 {len(calls) - 20} 次工具调用未展示 |")
    return rows


def _tool_artifact_excerpt_lines(tool_calls: Any) -> list[str]:
    """渲染工具 artifact 摘录。"""
    calls = list(tool_calls or [])
    lines: list[str] = []
    for call in calls[:10]:
        artifact = call.get("artifact")
        if artifact is None:
            continue
        lines.append(f"- `{call.get('name', '')}`: {_md(_truncate(stable_json_dumps(artifact), 500))}")
    return lines or ["无 artifact 摘录。"]


def _fmt(value: Any) -> str:
    """格式化指标值。"""
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _truncate(text: str, limit: int) -> str:
    """截断长文本。"""
    text = str(text or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _md(value: Any) -> str:
    """转义 Markdown 表格中的特殊字符。"""
    text = str(value)
    return text.replace("|", "\\|").replace("\n", " ")


__all__ = [
    "BaselineLLMReviewer",
    "ExperimentConfig",
    "SystemAgentReviewer",
    "build_dataset_snapshot",
    "build_metrics_summary",
    "build_paired_comparison",
    "create_result_dir",
    "run_experiment",
    "run_group",
    "sanitize_run_name",
    "select_samples",
    "write_experiment_report",
]
