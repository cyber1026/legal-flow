"""合同风险评测指标计算与报告生成。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eval.contract_clause_risk_review.io_utils import read_jsonl, write_json, write_jsonl
from eval.contract_clause_risk_review.schemas import LABEL_ORDER, LABELS, EvalLabel, PredictionRecord


def load_predictions(path: Path) -> list[PredictionRecord]:
    """从 JSONL 文件读取预测记录。"""
    records: list[PredictionRecord] = []
    for row in read_jsonl(path):
        records.append(
            PredictionRecord(
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
        )
    return records


def compute_metrics(records: list[PredictionRecord]) -> dict[str, Any]:
    """计算三分类、二分类和高风险漏检指标。"""
    valid = [record for record in records if record.success and record.predicted_label is not None]
    failed = [record for record in records if not record.success]
    confusion = _confusion_matrix(valid)
    per_label = {label: _label_metrics(valid, label) for label in LABELS}
    accuracy = _safe_div(
        sum(1 for record in valid if record.gold_label == record.predicted_label),
        len(valid),
    )
    macro_f1 = _safe_div(sum(item["f1"] for item in per_label.values()), len(LABELS))
    high_gold = [record for record in valid if record.gold_label == "high"]
    high_misses = [record for record in high_gold if record.predicted_label != "high"]
    high_to_safe = [record for record in high_gold if record.predicted_label == "safe"]
    safe_gold = [record for record in valid if record.gold_label == "safe"]
    safe_false_positives = [record for record in safe_gold if record.predicted_label != "safe"]
    severe_underestimates = [
        record for record in valid
        if LABEL_ORDER[record.predicted_label] < LABEL_ORDER[record.gold_label]  # type: ignore[index]
    ]
    risk_binary = _binary_risk_metrics(valid)
    return {
        "total": len(records),
        "valid": len(valid),
        "failed": len(failed),
        "accuracy": accuracy,
        "macro_f1": macro_f1,
        "per_label": per_label,
        "confusion_matrix": confusion,
        "safe_false_positive_rate": _safe_div(len(safe_false_positives), len(safe_gold)),
        "severe_underestimate_rate": _safe_div(len(severe_underestimates), len(valid)),
        "high_miss_rate": _safe_div(len(high_misses), len(high_gold)),
        "high_recall": 1.0 - _safe_div(len(high_misses), len(high_gold)) if high_gold else 0.0,
        "high_to_safe_rate": _safe_div(len(high_to_safe), len(high_gold)),
        "risk_binary": risk_binary,
        "engineering": {
            "failure_rate": _safe_div(len(failed), len(records)),
            "avg_elapsed_seconds": _safe_div(sum(r.elapsed_seconds for r in valid), len(valid)),
            "avg_opinion_count": _safe_div(sum(r.opinion_count for r in valid), len(valid)),
            "verified_citation_ratio": _safe_div(
                sum(r.verified_citation_count for r in valid),
                sum(r.citation_count for r in valid),
            ),
        },
    }


def save_metrics_report(records: list[PredictionRecord], results_dir: Path) -> dict[str, Any]:
    """保存 metrics.json、confusion_matrix.csv 和 failure_cases.md。"""
    metrics = compute_metrics(records)
    write_json(results_dir / "metrics.json", metrics)
    _write_confusion_csv(results_dir / "confusion_matrix.csv", metrics["confusion_matrix"])
    _write_failure_cases(results_dir / "failure_cases.md", records)
    return metrics


def _confusion_matrix(records: list[PredictionRecord]) -> dict[str, dict[str, int]]:
    """计算三分类混淆矩阵。"""
    matrix = {gold: {pred: 0 for pred in LABELS} for gold in LABELS}
    for record in records:
        if record.predicted_label is not None:
            matrix[record.gold_label][record.predicted_label] += 1
    return matrix


def _label_metrics(records: list[PredictionRecord], label: EvalLabel) -> dict[str, float]:
    """计算单个标签的 precision/recall/F1。"""
    tp = sum(1 for record in records if record.gold_label == label and record.predicted_label == label)
    fp = sum(1 for record in records if record.gold_label != label and record.predicted_label == label)
    fn = sum(1 for record in records if record.gold_label == label and record.predicted_label != label)
    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)
    return {
        "precision": precision,
        "recall": recall,
        "f1": _safe_div(2 * precision * recall, precision + recall),
        "support": float(sum(1 for record in records if record.gold_label == label)),
    }


def _binary_risk_metrics(records: list[PredictionRecord]) -> dict[str, float]:
    """计算 safe 与 risk 的二分类指标。"""
    tp = sum(1 for r in records if r.gold_label != "safe" and r.predicted_label != "safe")
    fp = sum(1 for r in records if r.gold_label == "safe" and r.predicted_label != "safe")
    fn = sum(1 for r in records if r.gold_label != "safe" and r.predicted_label == "safe")
    tn = sum(1 for r in records if r.gold_label == "safe" and r.predicted_label == "safe")
    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)
    return {
        "precision": precision,
        "recall": recall,
        "f1": _safe_div(2 * precision * recall, precision + recall),
        "accuracy": _safe_div(tp + tn, len(records)),
    }


def _safe_div(a: float, b: float) -> float:
    """安全除法，分母为 0 时返回 0。"""
    return 0.0 if b == 0 else round(a / b, 6)


def _write_confusion_csv(path: Path, matrix: dict[str, dict[str, int]]) -> None:
    """保存混淆矩阵 CSV。"""
    rows = [{"gold": gold, **{f"pred_{pred}": matrix[gold][pred] for pred in LABELS}} for gold in LABELS]
    write_jsonl(path.with_suffix(".jsonl"), rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write("gold,pred_safe,pred_medium,pred_high\n")
        for row in rows:
            f.write(f"{row['gold']},{row['pred_safe']},{row['pred_medium']},{row['pred_high']}\n")


def _write_failure_cases(path: Path, records: list[PredictionRecord]) -> None:
    """保存失败和严重低估样本摘要。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write("# Failure Cases\n\n")
        for record in records:
            if not record.success or (
                record.predicted_label is not None
                and LABEL_ORDER[record.predicted_label] < LABEL_ORDER[record.gold_label]
            ):
                f.write(f"- `{record.sample_id}` gold={record.gold_label} pred={record.predicted_label} error={record.error}\n")
