"""条款风险审查对照实验的目录、抽样和报告单测。"""

from __future__ import annotations

from pathlib import Path

from eval.contract_clause_risk_review.experiment import (
    ExperimentConfig,
    GroupReviewResult,
    build_dataset_snapshot,
    create_result_dir,
    infer_run_mode,
    resolve_sample_strategy,
    resolve_seed,
    run_experiment,
    sanitize_run_name,
    select_samples,
    write_experiment_report,
)
from eval.contract_clause_risk_review.io_utils import read_json, write_json, write_jsonl
from eval.contract_clause_risk_review.metrics import compute_metrics
from eval.contract_clause_risk_review.schemas import EvalLabel, EvalSample, PredictionRecord


def test_result_dir_generation_sanitizes_and_resolves_collision(tmp_path: Path) -> None:
    """默认结果目录只用时间戳；显式 run name 才追加名称。"""
    first = create_result_dir(tmp_path, timestamp="20260609-153012")
    second = create_result_dir(tmp_path, timestamp="20260609-153012")
    named = create_result_dir(tmp_path, "smoke 30/中文", timestamp="20260609-153013")

    assert first.name == "20260609-153012"
    assert second.name.startswith("20260609-153012_")
    assert named.name == "20260609-153013_smoke-30"
    assert first.exists()
    assert second.exists()
    assert sanitize_run_name("  ***  ") == ""


def test_auto_seed_strategy_and_run_mode_defaults() -> None:
    """默认 seed 使用当天日期，auto 抽样策略按 limit 自动解析。"""
    from datetime import datetime

    today = int(datetime.now().astimezone().strftime("%Y%m%d"))

    assert resolve_seed(None) == today
    assert resolve_seed(123) == 123
    assert resolve_sample_strategy("auto", 30) == "stratified"
    assert resolve_sample_strategy("auto", None) == "all"
    assert infer_run_mode(30) == "smoke"
    assert infer_run_mode(None) == "full"


def test_stratified_sampling_keeps_balanced_labels() -> None:
    """分层 smoke 抽样 30 条时应三类各 10 条，且 seed 固定后稳定。"""
    samples = _samples_per_label(15)

    selected1 = select_samples(samples, strategy="stratified", limit=30, seed=20260609)
    selected2 = select_samples(samples, strategy="stratified", limit=30, seed=20260609)
    counts = {label: sum(1 for sample in selected1 if sample.gold_label == label) for label in ("safe", "medium", "high")}

    assert counts == {"safe": 10, "medium": 10, "high": 10}
    assert [sample.sample_id for sample in selected1] == [sample.sample_id for sample in selected2]


def test_dataset_snapshot_saves_hash_distribution_and_manifest_summary(tmp_path: Path) -> None:
    """dataset_snapshot 应记录 hash、样本分布和 manifest 摘要。"""
    dataset_path = tmp_path / "contract_risk_single_clause.jsonl"
    samples = _samples_per_label(1)
    write_jsonl(dataset_path, [sample.to_dict() for sample in samples])
    write_json(
        tmp_path / "contract_risk_manifest.json",
        {
            "counts": {"samples": 3},
            "selection_gaps": {"missing_contracts": ["煤炭买卖合同"]},
            "prompt_hashes": {"risk_injection": "abc"},
        },
    )

    snapshot = build_dataset_snapshot(dataset_path, samples)

    assert snapshot["sample_count"] == 3
    assert len(snapshot["dataset_sha256"]) == 64
    assert snapshot["label_distribution"] == {"safe": 1, "medium": 1, "high": 1}
    assert snapshot["manifest_summary"]["counts"] == {"samples": 3}


def test_metrics_include_requested_binary_and_level_values() -> None:
    """指标应同时支持二分类风险识别和三分类风险等级准确率。"""
    records = [
        PredictionRecord(sample_id="s1", gold_label="safe", predicted_label="safe", predicted_system_level="none"),
        PredictionRecord(sample_id="s2", gold_label="medium", predicted_label="high", predicted_system_level="high"),
        PredictionRecord(sample_id="s3", gold_label="high", predicted_label="safe", predicted_system_level="none"),
    ]

    metrics = compute_metrics(records)

    assert metrics["risk_binary"]["accuracy"] == 0.666667
    assert metrics["risk_binary"]["precision"] == 1.0
    assert metrics["risk_binary"]["recall"] == 0.5
    assert metrics["accuracy"] == 0.333333
    assert metrics["high_recall"] == 0.0


def test_markdown_report_contains_group_and_delta_tables(tmp_path: Path) -> None:
    """Markdown 报告应包含两组结果和差值表。"""
    path = tmp_path / "experiment_report.md"
    sample = _sample("s1", "high")
    metrics_summary = {
        "baseline_llm": {
            "accuracy": 0.5,
            "risk_precision": 0.5,
            "risk_recall": 0.5,
            "risk_f1": 0.5,
            "high_risk_recall": 0.0,
            "risk_level_accuracy": 0.5,
            "failure_rate": 0.0,
            "avg_latency": 1.0,
            "tool_stats": {"avg_tool_calls": 0.0},
        },
        "system_agent": {
            "accuracy": 1.0,
            "risk_precision": 1.0,
            "risk_recall": 1.0,
            "risk_f1": 1.0,
            "high_risk_recall": 1.0,
            "risk_level_accuracy": 1.0,
            "failure_rate": 0.0,
            "avg_latency": 2.0,
            "tool_stats": {"avg_tool_calls": 2.0},
        },
        "delta_system_minus_baseline": {"accuracy": 0.5, "risk_level_accuracy": 0.5},
    }
    write_experiment_report(
        path,
        run_config={"run_name": "smoke", "sample_strategy": "all"},
        snapshot={"dataset_path": "dataset.jsonl", "dataset_sha256": "abc", "sample_count": 1, "label_distribution": {"high": 1}},
        metrics_summary=metrics_summary,
        confusion_matrices={
            "baseline_llm": {"safe": {"safe": 0, "medium": 0, "high": 0}, "medium": {"safe": 0, "medium": 0, "high": 0}, "high": {"safe": 1, "medium": 0, "high": 0}},
            "system_agent": {"safe": {"safe": 0, "medium": 0, "high": 0}, "medium": {"safe": 0, "medium": 0, "high": 0}, "high": {"safe": 0, "medium": 0, "high": 1}},
        },
        paired={"improved": [{"sample_id": "s1", "gold_label": "high", "baseline_predicted_label": "safe", "system_predicted_label": "high"}]},
        sample_by_id={"s1": sample},
    )

    text = path.read_text(encoding="utf-8")

    assert "| Group | Accuracy | Risk Precision | Risk Recall | Risk F1 | High Risk Recall | Risk Level Accuracy |" in text
    assert "baseline_llm" in text
    assert "system_agent" in text
    assert "system - baseline" in text


def test_run_experiment_writes_json_and_markdown_outputs(tmp_path: Path) -> None:
    """完整 experiment 流程应在结果目录落盘 JSON、JSONL、trace 和 Markdown。"""
    dataset_path = tmp_path / "contract_risk_single_clause.jsonl"
    samples = _samples_per_label(1)
    write_jsonl(dataset_path, [sample.to_dict() for sample in samples])
    write_json(dataset_path.parent / "contract_risk_manifest.json", {"counts": {"samples": 3}})
    result_dir = tmp_path / "results" / "fixed"
    config = ExperimentConfig(
        dataset_path=dataset_path,
        result_dir=result_dir,
        sample_strategy="auto",
        limit=3,
        seed=20260609,
        cache_dir=tmp_path / "cache",
        force=True,
        show_progress=False,
    )

    async def baseline(sample: EvalSample) -> GroupReviewResult:
        """模拟纯 LLM 预测。"""
        label = "safe"
        return _fake_result(sample, label, "baseline_llm")

    async def system(sample: EvalSample) -> GroupReviewResult:
        """模拟系统 agent 预测。"""
        return _fake_result(sample, sample.gold_label, "system_agent", tool_calls=1)

    manifest = asyncio_run(run_experiment(config, baseline_reviewer=baseline, system_reviewer=system))

    assert manifest["groups"]["baseline_llm"]["total"] == 3
    run_config = read_json(result_dir / "run_config.json")
    assert run_config["run_mode"] == "smoke"
    assert run_config["effective_sample_strategy"] == "stratified"
    assert run_config["effective_seed"] == 20260609
    assert (result_dir / "run_config.json").exists()
    assert (result_dir / "dataset_snapshot.json").exists()
    assert (result_dir / "metrics_summary.json").exists()
    assert (result_dir / "paired_comparison.json").exists()
    assert (result_dir / "confusion_matrices.json").exists()
    assert (result_dir / "experiment_report.md").exists()
    assert (result_dir / "groups" / "baseline_llm" / "predictions.jsonl").exists()
    assert (result_dir / "groups" / "system_agent" / "traces" / "s-high-0.json").exists()
    assert (result_dir / "case_reports" / "s-high-0.md").exists()
    metrics = read_json(result_dir / "metrics_summary.json")
    assert metrics["system_agent"]["risk_level_accuracy"] == 1.0


def _samples_per_label(count: int) -> list[EvalSample]:
    """构造每个标签固定数量的样本。"""
    samples: list[EvalSample] = []
    for label in ("safe", "medium", "high"):
        for index in range(count):
            samples.append(_sample(f"s-{label}-{index}", label))
    return samples


def _sample(sample_id: str, label: EvalLabel) -> EvalSample:
    """构造单个测试样本。"""
    return EvalSample(
        sample_id=sample_id,
        gold_label=label,
        text=f"{label} 测试条款，约定付款、交付和违约责任。",
        contract_name="测试合同",
        source_path="test.md",
        seed_candidate_id=sample_id,
        clause_no="第一条",
    )


def _fake_result(sample: EvalSample, predicted: EvalLabel, group: str, *, tool_calls: int = 0) -> GroupReviewResult:
    """构造 fake 实验组结果。"""
    system_level = {"safe": "none", "medium": "medium", "high": "high"}[predicted]
    review = {
        "has_opinion": predicted != "safe",
        "opinions": [],
        "risk_assessment": {
            "risk_level": system_level,
            "rationale": f"预测为 {predicted}",
            "affected_party": "双方",
            "confidence": 0.9,
        },
        "consistency_facts": [],
        "note": "",
    }
    trace_tool_calls = [
        {
            "name": "search_law",
            "args": {"query": "违约责任"},
            "citations": [{"law_name": "民法典", "article_no": "第五百八十五条"}],
            "result_preview": "检索结果",
        }
        for _ in range(tool_calls)
    ]
    return GroupReviewResult(
        prediction=PredictionRecord(
            sample_id=sample.sample_id,
            gold_label=sample.gold_label,
            predicted_label=predicted,
            predicted_system_level=system_level,
            raw_review=review,
        ),
        trace={"sample_id": sample.sample_id, "group": group, "tool_calls": trace_tool_calls, "review": review},
    )


def asyncio_run(coro):
    """隔离 pytest 对 asyncio 插件的依赖。"""
    import asyncio

    return asyncio.run(coro)
