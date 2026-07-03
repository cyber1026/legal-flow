"""合同风险评测的缓存、runner 和指标单测。"""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.contract_clause_risk_review.cache import JsonFileCache
from eval.contract_clause_risk_review.dataset_builder import BuildConfig, DatasetBuilder
from eval.contract_clause_risk_review.io_utils import write_jsonl
from eval.contract_clause_risk_review.metrics import compute_metrics
from eval.contract_clause_risk_review.runner import ReviewRunner
from eval.contract_clause_risk_review.schemas import CandidateClause, EvalSample, PredictionRecord, RuleFilterResult


def test_json_cache_key_and_roundtrip(tmp_path: Path) -> None:
    """缓存 key 应稳定，写入后能按任务和 key 读回。"""
    cache = JsonFileCache(tmp_path)
    payload = {"text": "甲方应付款"}
    key1 = cache.build_key(task="t", prompt_hash="p", input_payload=payload, model="m")
    key2 = cache.build_key(task="t", prompt_hash="p", input_payload=payload, model="m")

    cache.set("t", key1, model="m", prompt_hash="p", input_payload=payload, output={"ok": True})
    cached = cache.get("t", key2)

    assert key1 == key2
    assert cached is not None
    assert cached["output"] == {"ok": True}


def test_metrics_high_miss_and_safe_false_positive() -> None:
    """指标应正确统计高风险漏检和 safe 误报。"""
    records = [
        PredictionRecord(sample_id="s1", gold_label="high", predicted_label="safe", predicted_system_level="none"),
        PredictionRecord(sample_id="s2", gold_label="high", predicted_label="high", predicted_system_level="high"),
        PredictionRecord(sample_id="s3", gold_label="safe", predicted_label="medium", predicted_system_level="medium"),
        PredictionRecord(sample_id="s4", gold_label="medium", predicted_label="medium", predicted_system_level="medium"),
    ]

    metrics = compute_metrics(records)

    assert metrics["high_miss_rate"] == 0.5
    assert metrics["high_to_safe_rate"] == 0.5
    assert metrics["safe_false_positive_rate"] == 1.0
    assert metrics["risk_binary"]["recall"] == pytest.approx(2 / 3, rel=1e-6)


def test_runner_resumes_existing_predictions(tmp_path: Path) -> None:
    """runner 应跳过已有 sample_id，只补跑缺失预测。"""
    dataset_path = tmp_path / "dataset.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    samples = [
        EvalSample(
            sample_id="s1",
            gold_label="safe",
            text="标准条款",
            contract_name="合同一",
            source_path="a.md",
            seed_candidate_id="c1",
        ),
        EvalSample(
            sample_id="s2",
            gold_label="high",
            text="高风险条款",
            contract_name="合同一",
            source_path="a.md",
            seed_candidate_id="c2",
        ),
    ]
    write_jsonl(dataset_path, [sample.to_dict() for sample in samples])
    write_jsonl(
        predictions_path,
        [
            PredictionRecord(
                sample_id="s1",
                gold_label="safe",
                predicted_label="safe",
                predicted_system_level="none",
            ).to_dict()
        ],
    )
    calls: list[str] = []

    async def fake_reviewer(sample: EvalSample) -> dict:
        """模拟系统审查入口。"""
        calls.append(sample.sample_id)
        return {
            "has_opinion": True,
            "opinions": [],
            "risk_assessment": {"risk_level": "high"},
        }

    runner = ReviewRunner(cache=JsonFileCache(tmp_path / "cache"), reviewer=fake_reviewer)
    records = asyncio_run(runner.run_dataset(dataset_path, predictions_path))

    assert calls == ["s2"]
    assert len(records) == 2
    assert {record.sample_id for record in records} == {"s1", "s2"}


def test_dataset_builder_retries_rejected_variant(tmp_path: Path) -> None:
    """风险注入校验不一致时应按配置重试并保留通过样本。"""
    attempts: list[dict] = []

    class FakeClient:
        """模拟 LLM JSON 客户端。"""

        async def complete_json(self, *, task: str, prompt: str, prompt_hash: str, input_payload: dict) -> dict:
            """根据输入返回可预测的注入和校验结果。"""
            if task == "risk_injection":
                attempts.append(dict(input_payload))
                target = input_payload["target_label"]
                if target == "medium" and input_payload.get("attempt") == 2:
                    return {
                        "injected_clause": "中等风险条款",
                        "target_label": "medium",
                        "risk_pattern": "中风险",
                        "expected_issue": "补充约定不足",
                    }
                if target == "medium":
                    return {
                        "injected_clause": "过高风险条款",
                        "target_label": "medium",
                        "risk_pattern": "过高风险",
                        "expected_issue": "重大权利丧失",
                    }
                return {
                    "injected_clause": "高风险条款",
                    "target_label": "high",
                    "risk_pattern": "高风险",
                    "expected_issue": "重大权利丧失",
                }
            if task == "risk_validation":
                clause = input_payload["injected_clause"]
                if clause == "过高风险条款":
                    return {"accepted": False, "label": "high", "reason": "风险过高", "confidence": 0.9}
                if clause == "中等风险条款":
                    return {"accepted": True, "label": "medium", "reason": "符合中风险", "confidence": 0.9}
                return {"accepted": True, "label": "high", "reason": "符合高风险", "confidence": 0.9}
            raise AssertionError(task)

    candidate = CandidateClause(
        candidate_id="c1",
        contract_name="测试合同",
        source_path="a.md",
        source_index=1,
        clause_id="c1",
        clause_no="第一条",
        title="付款",
        section_path="",
        text="买受人应在验收合格后三十日内支付货款。",
        rule_filter=RuleFilterResult(passed=True, score=6, dimensions=["付款价款"]),
    )
    config = BuildConfig(
        datasets_dir=tmp_path / "datasets",
        cache_dir=tmp_path / "cache",
        force_stages={"dataset", "generate"},
        generation_attempts=2,
    )
    builder = DatasetBuilder(config, llm_client=FakeClient())

    samples = asyncio_run(builder.build_dataset([candidate]))

    medium = next(sample for sample in samples if sample.gold_label == "medium")
    assert medium.text == "中等风险条款"
    assert medium.generation["attempt"] == 2
    assert any(payload.get("attempt") == 2 for payload in attempts)


def asyncio_run(coro):
    """隔离 pytest 对 asyncio 插件的依赖。"""
    import asyncio

    return asyncio.run(coro)
