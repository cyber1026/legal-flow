"""合同条款风险评测数据集构建流程。"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from eval.contract_clause_risk_review.cache import JsonFileCache
from eval.contract_clause_risk_review.filters import build_candidates_from_contract, group_candidates_by_contract
from eval.contract_clause_risk_review.io_utils import read_jsonl, write_json, write_jsonl
from eval.contract_clause_risk_review.llm_client import LLMJsonClient
from eval.contract_clause_risk_review.prompts import (
    CLAUSE_USEFULNESS_PROMPT,
    RISK_INJECTION_PROMPT,
    RISK_VALIDATION_PROMPT,
    build_clause_usefulness_input,
    build_risk_injection_input,
    build_validation_input,
    prompt_hash,
)
from eval.contract_clause_risk_review.schemas import CandidateClause, EvalLabel, EvalSample


class JsonCompletionClient(Protocol):
    """数据集构建所需的 LLM JSON 客户端协议。"""

    async def complete_json(
        self,
        *,
        task: str,
        prompt: str,
        prompt_hash: str,
        input_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """生成结构化 JSON。"""


@dataclass(slots=True)
class BuildConfig:
    """数据集构建配置。"""

    source_dir: Path = Path("data/legal_sources/layer4_standard_contracts/samr/sales_contracts_100")
    datasets_dir: Path = Path("eval/contract_clause_risk_review/datasets")
    cache_dir: Path = Path("eval/contract_clause_risk_review/cache")
    per_contract: int = 3
    llm_prefilter_per_contract: int = 12
    max_clause_chars: int = 1600
    limit_contracts: int | None = None
    llm_filter: bool = True
    generate_variants: bool = True
    generation_attempts: int = 2
    concurrency: int = 4
    force_stages: set[str] = field(default_factory=set)

    @property
    def candidate_path(self) -> Path:
        """候选条款文件路径。"""
        return self.datasets_dir / "candidate_clauses.jsonl"

    @property
    def selected_path(self) -> Path:
        """已选标准条款文件路径。"""
        return self.datasets_dir / "selected_safe_clauses.jsonl"

    @property
    def dataset_path(self) -> Path:
        """主评测集文件路径。"""
        return self.datasets_dir / "contract_risk_single_clause.jsonl"

    @property
    def high_recall_path(self) -> Path:
        """高风险子集文件路径。"""
        return self.datasets_dir / "contract_risk_high_recall.jsonl"

    @property
    def rejected_path(self) -> Path:
        """被拒绝生成样本文件路径。"""
        return self.datasets_dir / "rejected_generated_samples.jsonl"

    @property
    def manifest_path(self) -> Path:
        """构建清单文件路径。"""
        return self.datasets_dir / "contract_risk_manifest.json"


class DatasetBuilder:
    """合同条款风险评测数据集构建器。"""

    def __init__(
        self,
        config: BuildConfig,
        *,
        llm_client: JsonCompletionClient | None = None,
    ) -> None:
        """初始化构建器。"""
        self.config = config
        self.cache = JsonFileCache(config.cache_dir)
        self.llm_client = llm_client or LLMJsonClient(
            cache=self.cache,
            force_tasks=config.force_stages,
        )

    def build_candidates(self) -> list[CandidateClause]:
        """抽取并保存候选条款。"""
        if self.config.candidate_path.exists() and "candidates" not in self.config.force_stages:
            return [CandidateClause.from_dict(row) for row in read_jsonl(self.config.candidate_path)]

        paths = sorted(self.config.source_dir.glob("*.md"))
        if self.config.limit_contracts is not None:
            paths = paths[: self.config.limit_contracts]
        candidates: list[CandidateClause] = []
        for path in paths:
            candidates.extend(
                build_candidates_from_contract(
                    path,
                    max_clause_chars=self.config.max_clause_chars,
                )
            )
        write_jsonl(self.config.candidate_path, [candidate.to_dict() for candidate in candidates])
        return candidates

    async def select_safe_clauses(self, candidates: list[CandidateClause]) -> list[CandidateClause]:
        """用规则和可选 LLM 选出每份合同的标准 safe seed 条款。"""
        if self.config.selected_path.exists() and "select" not in self.config.force_stages:
            return [CandidateClause.from_dict(row) for row in read_jsonl(self.config.selected_path)]

        rule_passed = [candidate for candidate in candidates if candidate.rule_filter.passed]
        llm_pool = _build_llm_screening_pool(
            rule_passed,
            per_contract=max(self.config.per_contract, self.config.llm_prefilter_per_contract),
        )
        if self.config.llm_filter:
            await self._apply_llm_usefulness_filter(llm_pool)
        else:
            for candidate in llm_pool:
                candidate.llm_filter = {
                    "decision": "useful",
                    "risk_dimensions": candidate.rule_filter.dimensions,
                    "reason": "规则筛选通过，未启用 LLM 语义筛选",
                    "confidence": 1.0,
                }

        selected: list[CandidateClause] = []
        for _contract_name, items in sorted(group_candidates_by_contract(llm_pool).items()):
            useful = [item for item in items if _is_llm_useful(item)]
            chosen = _select_diverse(useful, self.config.per_contract)
            if len(chosen) < self.config.per_contract:
                fallback_pool = [
                    item for item in items
                    if item not in chosen and _is_rule_fallback_candidate(item)
                ]
                fallback = _select_diverse(fallback_pool, self.config.per_contract - len(chosen))
                for item in fallback:
                    item.llm_filter = {
                        "decision": "useful_by_rule_fallback",
                        "risk_dimensions": item.rule_filter.dimensions,
                        "reason": "LLM 有效条款不足 3 条，按高规则分和实质风险维度兜底选入",
                        "confidence": 0.55,
                        "llm_decision": item.llm_filter,
                    }
                chosen.extend(fallback)
            selected.extend(chosen)
        write_jsonl(self.config.selected_path, [candidate.to_dict() for candidate in selected])
        return selected

    async def _apply_llm_usefulness_filter(self, candidates: list[CandidateClause]) -> None:
        """并发调用 LLM 判断候选条款是否有评测价值。"""
        semaphore = asyncio.Semaphore(max(1, self.config.concurrency))
        prompt_digest = prompt_hash(CLAUSE_USEFULNESS_PROMPT)

        async def judge(candidate: CandidateClause) -> None:
            async with semaphore:
                payload = build_clause_usefulness_input(
                    contract_name=candidate.contract_name,
                    clause_no=candidate.clause_no,
                    title=candidate.title,
                    section_path=candidate.section_path,
                    text=candidate.text,
                    rule_dimensions=candidate.rule_filter.dimensions,
                )
                try:
                    candidate.llm_filter = await self.llm_client.complete_json(
                        task="clause_usefulness",
                        prompt=CLAUSE_USEFULNESS_PROMPT,
                        prompt_hash=prompt_digest,
                        input_payload=payload,
                    )
                except Exception as exc:
                    candidate.llm_filter = {
                        "decision": "not_useful",
                        "risk_dimensions": candidate.rule_filter.dimensions,
                        "reason": "LLM 条款有效性判断失败，保守排除",
                        "confidence": 0.0,
                        "error": f"{type(exc).__name__}: {exc}",
                    }

        await asyncio.gather(*(judge(candidate) for candidate in candidates))

    async def build_dataset(self, selected: list[CandidateClause]) -> list[EvalSample]:
        """基于 safe seed 构建完整三分类评测集。"""
        rebuild = bool({"generate", "dataset"} & self.config.force_stages)
        existing = {} if rebuild else {
            row["sample_id"]: EvalSample.from_dict(row)
            for row in read_jsonl(self.config.dataset_path)
        }
        rejected_rows = [] if rebuild else list(read_jsonl(self.config.rejected_path))

        for candidate in selected:
            safe = _safe_sample(candidate)
            existing.setdefault(safe.sample_id, safe)

        if self.config.generate_variants:
            generated, rejected = await self._generate_variants(selected, existing)
            for sample in generated:
                existing[sample.sample_id] = sample
            rejected_rows.extend(rejected)

        samples = [existing[key] for key in sorted(existing)]
        write_jsonl(self.config.dataset_path, [sample.to_dict() for sample in samples])
        high_samples = [sample.to_dict() for sample in samples if sample.gold_label == "high"]
        write_jsonl(self.config.high_recall_path, high_samples)
        if rejected_rows:
            write_jsonl(self.config.rejected_path, rejected_rows)
        return samples

    async def _generate_variants(
        self,
        selected: list[CandidateClause],
        existing: dict[str, EvalSample],
    ) -> tuple[list[EvalSample], list[dict[str, Any]]]:
        """为每个 seed 生成 medium/high 风险变体。"""
        semaphore = asyncio.Semaphore(max(1, self.config.concurrency))
        injection_hash = prompt_hash(RISK_INJECTION_PROMPT)
        validation_hash = prompt_hash(RISK_VALIDATION_PROMPT)
        generated: list[EvalSample] = []
        rejected: list[dict[str, Any]] = []

        async def make_one(candidate: CandidateClause, target: EvalLabel) -> None:
            sample_id = f"{candidate.candidate_id}-{target}"
            if sample_id in existing and "generate" not in self.config.force_stages:
                return
            async with semaphore:
                retry_note = ""
                last_rejection: dict[str, Any] | None = None
                attempts = max(1, self.config.generation_attempts)
                for attempt in range(1, attempts + 1):
                    injection_payload = build_risk_injection_input(
                        target_label=target,
                        contract_name=candidate.contract_name,
                        clause_no=candidate.clause_no,
                        title=candidate.title,
                        text=candidate.text,
                        attempt=attempt,
                        retry_note=retry_note,
                    )
                    try:
                        injection = await self.llm_client.complete_json(
                            task="risk_injection",
                            prompt=RISK_INJECTION_PROMPT,
                            prompt_hash=injection_hash,
                            input_payload=injection_payload,
                        )
                    except Exception as exc:
                        last_rejection = {
                            "sample_id": sample_id,
                            "candidate_id": candidate.candidate_id,
                            "target_label": target,
                            "attempt": attempt,
                            "reason": "risk_injection_failed",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                        break
                    validation_payload = build_validation_input(
                        target_label=target,
                        injected_clause=str(injection.get("injected_clause", "")),
                        risk_pattern=str(injection.get("risk_pattern", "")),
                        expected_issue=str(injection.get("expected_issue", "")),
                    )
                    try:
                        validation = await self.llm_client.complete_json(
                            task="risk_validation",
                            prompt=RISK_VALIDATION_PROMPT,
                            prompt_hash=validation_hash,
                            input_payload=validation_payload,
                        )
                    except Exception as exc:
                        last_rejection = {
                            "sample_id": sample_id,
                            "candidate_id": candidate.candidate_id,
                            "target_label": target,
                            "attempt": attempt,
                            "injection": injection,
                            "reason": "risk_validation_failed",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                        break
                    if _validation_accepts(validation, target):
                        generated.append(_variant_sample(candidate, target, injection, validation, attempt=attempt))
                        return
                    last_rejection = {
                        "sample_id": sample_id,
                        "candidate_id": candidate.candidate_id,
                        "target_label": target,
                        "attempt": attempt,
                        "injection": injection,
                        "validation": validation,
                        "reason": "validation_rejected_or_label_mismatch",
                    }
                    retry_note = _build_retry_note(target, validation)
                if last_rejection is not None:
                    last_rejection["attempts"] = attempts
                    rejected.append(last_rejection)

        tasks = [
            make_one(candidate, target)
            for candidate in selected
            for target in ("medium", "high")
        ]
        await asyncio.gather(*tasks)
        return generated, rejected

    async def build_all(self) -> dict[str, Any]:
        """执行完整数据集构建流程并写入 manifest。"""
        started = time.perf_counter()
        candidates = self.build_candidates()
        selected = await self.select_safe_clauses(candidates)
        samples = await self.build_dataset(selected)
        manifest = build_manifest(
            config=self.config,
            candidates=candidates,
            selected=selected,
            samples=samples,
            cache_stats=self.cache.stats(),
            elapsed_seconds=round(time.perf_counter() - started, 3),
        )
        write_json(self.config.manifest_path, manifest)
        return manifest


def _is_llm_useful(candidate: CandidateClause) -> bool:
    """判断 LLM 筛选是否认为候选条款有评测价值。"""
    decision = str((candidate.llm_filter or {}).get("decision", "")).strip().lower()
    return decision == "useful"


def _is_rule_fallback_candidate(candidate: CandidateClause) -> bool:
    """判断候选条款是否可作为 LLM 不足时的保守规则兜底。"""
    if not candidate.rule_filter.passed:
        return False
    if candidate.rule_filter.score < 3:
        return False
    if not candidate.rule_filter.dimensions:
        return False
    text = candidate.text.strip()
    if len(text) < 50:
        return False
    bad_terms = (
        "签章",
        "监制",
        "签约地点",
        "签订地点",
        "签订时间",
        "卖方（甲方）",
        "买方（乙方）",
        "甲方（买方）",
        "乙方（卖方）",
        "开户银行",
        "邮政编码",
        "通讯地址",
        "法定代表人",
        "委托代理人",
        "代表人",
        "监制部门",
        "印制单位",
    )
    return not any(term in text for term in bad_terms)


def _select_diverse(candidates: list[CandidateClause], limit: int) -> list[CandidateClause]:
    """按分数和维度多样性选择条款。"""
    ordered = sorted(
        candidates,
        key=lambda item: (item.rule_filter.score, -item.source_index),
        reverse=True,
    )
    selected: list[CandidateClause] = []
    used_dimensions: set[str] = set()
    for candidate in ordered:
        dimensions = set(candidate.rule_filter.dimensions)
        if len(selected) >= limit:
            break
        if not selected or not dimensions <= used_dimensions:
            selected.append(candidate)
            used_dimensions.update(dimensions)
    for candidate in ordered:
        if len(selected) >= limit:
            break
        if candidate not in selected:
            selected.append(candidate)
    return selected[:limit]


def _build_llm_screening_pool(candidates: list[CandidateClause], *, per_contract: int) -> list[CandidateClause]:
    """按合同分组选择送 LLM 语义筛选的 top 候选池。"""
    pool: list[CandidateClause] = []
    for _contract_name, items in sorted(group_candidates_by_contract(candidates).items()):
        pool.extend(_select_diverse(items, per_contract))
    return pool


def _safe_sample(candidate: CandidateClause) -> EvalSample:
    """把标准合同条款转换成 safe 样本。"""
    return EvalSample(
        sample_id=f"{candidate.candidate_id}-safe",
        gold_label="safe",
        text=candidate.text,
        contract_name=candidate.contract_name,
        source_path=candidate.source_path,
        seed_candidate_id=candidate.candidate_id,
        clause_no=candidate.clause_no,
        title=candidate.title,
        section_path=candidate.section_path,
        variant_type="safe",
        generation={"source": "standard_contract", "llm_filter": candidate.llm_filter},
    )


def _variant_sample(
    candidate: CandidateClause,
    target: EvalLabel,
    injection: dict[str, Any],
    validation: dict[str, Any],
    *,
    attempt: int = 1,
) -> EvalSample:
    """把 LLM 注入结果转换成风险样本。"""
    return EvalSample(
        sample_id=f"{candidate.candidate_id}-{target}",
        gold_label=target,
        text=str(injection.get("injected_clause", "")),
        contract_name=candidate.contract_name,
        source_path=candidate.source_path,
        seed_candidate_id=candidate.candidate_id,
        clause_no=candidate.clause_no,
        title=candidate.title,
        section_path=candidate.section_path,
        variant_type=f"injected_{target}",
        risk_pattern=str(injection.get("risk_pattern", "")),
        expected_issue=str(injection.get("expected_issue", "")),
        generation={"injection": injection, "validation": validation, "attempt": attempt},
    )


def _validation_accepts(validation: dict[str, Any], target: EvalLabel) -> bool:
    """判断复核结果是否接受生成样本。"""
    accepted = bool(validation.get("accepted"))
    label = str(validation.get("label", "")).strip().lower()
    return accepted and label == target


def _build_retry_note(target: EvalLabel, validation: dict[str, Any]) -> str:
    """根据校验失败结果生成下一次风险注入的纠偏说明。"""
    label = str(validation.get("label", "")).strip().lower() or "unknown"
    reason = str(validation.get("reason", "")).strip()
    if target == "medium":
        guidance = "上一版未被接受；请生成更清晰的中风险，避免升级到高风险，也不要弱化成 safe。"
    else:
        guidance = "上一版未被接受；请生成更明确的高风险，使重大权利丧失、责任严重失衡或关键救济缺失更突出。"
    return f"{guidance} 上次复核标签为 {label}，理由：{reason}"


def build_manifest(
    *,
    config: BuildConfig,
    candidates: list[CandidateClause],
    selected: list[CandidateClause],
    samples: list[EvalSample],
    cache_stats: dict[str, int],
    elapsed_seconds: float,
) -> dict[str, Any]:
    """构建数据集 manifest。"""
    rule_passed = sum(1 for candidate in candidates if candidate.rule_filter.passed)
    selection_useful = sum(1 for candidate in candidates if _is_llm_useful(candidate))
    fallback_useful = sum(
        1 for candidate in selected
        if str((candidate.llm_filter or {}).get("decision", "")).strip().lower() == "useful_by_rule_fallback"
    )
    llm_screened = sum(1 for candidate in candidates if candidate.llm_filter)
    llm_useful = selection_useful if config.llm_filter else 0
    by_label: dict[str, int] = {"safe": 0, "medium": 0, "high": 0}
    for sample in samples:
        by_label[sample.gold_label] = by_label.get(sample.gold_label, 0) + 1
    source_contracts = sorted({candidate.contract_name for candidate in candidates})
    selected_by_contract = Counter(candidate.contract_name for candidate in selected)
    missing_contracts = [name for name in source_contracts if selected_by_contract.get(name, 0) == 0]
    underfilled_contracts = {
        name: count
        for name, count in sorted(selected_by_contract.items())
        if count < config.per_contract
    }
    return {
        "source_dir": str(config.source_dir),
        "datasets_dir": str(config.datasets_dir),
        "cache_dir": str(config.cache_dir),
        "per_contract": config.per_contract,
        "llm_prefilter_per_contract": config.llm_prefilter_per_contract,
        "limit_contracts": config.limit_contracts,
        "llm_filter": config.llm_filter,
        "generate_variants": config.generate_variants,
        "generation_attempts": config.generation_attempts,
        "risk_label_scale": ["safe", "medium", "high"],
        "system_label_mapping": {
            "none": "safe",
            "low": "safe",
            "medium": "medium",
            "high": "high",
            "critical": "high",
        },
        "prompt_hashes": {
            "clause_usefulness": prompt_hash(CLAUSE_USEFULNESS_PROMPT),
            "risk_injection": prompt_hash(RISK_INJECTION_PROMPT),
            "risk_validation": prompt_hash(RISK_VALIDATION_PROMPT),
        },
        "counts": {
            "candidate_clauses": len(candidates),
            "rule_passed": rule_passed,
            "rule_filtered": len(candidates) - rule_passed,
            "llm_screened": llm_screened,
            "llm_useful": llm_useful,
            "selection_useful": selection_useful,
            "fallback_useful": fallback_useful,
            "selected_safe": len(selected),
            "samples": len(samples),
            "samples_by_label": by_label,
            "source_contracts": len(source_contracts),
            "selected_contracts": len(selected_by_contract),
            "missing_contracts": len(missing_contracts),
            "underfilled_contracts": len(underfilled_contracts),
        },
        "selection_gaps": {
            "missing_contracts": missing_contracts,
            "underfilled_contracts": underfilled_contracts,
        },
        "cache": cache_stats,
        "elapsed_seconds": elapsed_seconds,
    }
