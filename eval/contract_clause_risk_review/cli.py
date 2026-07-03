"""合同风险评测命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from eval.contract_clause_risk_review.cache import JsonFileCache
from eval.contract_clause_risk_review.dataset_builder import BuildConfig, DatasetBuilder
from eval.contract_clause_risk_review.io_utils import ensure_dir, write_json
from eval.contract_clause_risk_review.llm_client import LLMJsonClient
from eval.contract_clause_risk_review.metrics import load_predictions, save_metrics_report
from eval.contract_clause_risk_review.runner import ReviewRunner
from eval.contract_clause_risk_review.experiment import ExperimentConfig, run_experiment


def main(argv: list[str] | None = None) -> int:
    """解析命令行参数并执行对应子命令。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "build":
        manifest = asyncio.run(_cmd_build(args))
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "run":
        result = asyncio.run(_cmd_run(args))
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "metrics":
        metrics = _cmd_metrics(args)
        print(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "experiment":
        manifest = asyncio.run(_cmd_experiment(args))
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    """构建 argparse 解析器。"""
    defaults = BuildConfig()
    parser = argparse.ArgumentParser(description="合同条款风险评测工具")
    sub = parser.add_subparsers(dest="command")

    build = sub.add_parser("build", help="构建评测数据集")
    build.add_argument("--source-dir", type=Path, default=defaults.source_dir)
    build.add_argument("--datasets-dir", type=Path, default=defaults.datasets_dir)
    build.add_argument("--cache-dir", type=Path, default=defaults.cache_dir)
    build.add_argument("--per-contract", type=int, default=3)
    build.add_argument("--llm-prefilter-per-contract", type=int, default=defaults.llm_prefilter_per_contract)
    build.add_argument("--limit-contracts", type=int, default=None)
    build.add_argument("--concurrency", type=int, default=4)
    build.add_argument("--generation-attempts", type=int, default=defaults.generation_attempts)
    build.add_argument("--no-llm-filter", action="store_true", help="只使用规则筛选，不调用 LLM 判断条款有效性")
    build.add_argument("--skip-generation", action="store_true", help="只构建 safe seed，不生成 medium/high 变体")
    build.add_argument("--force", default="", help="逗号分隔强制重跑阶段：candidates,select,generate 或 all")
    build.add_argument("--force-cache", default="", help="逗号分隔强制忽略 LLM 缓存的任务：clause_usefulness,risk_injection,risk_validation 或 all")
    build.add_argument("--provider", default=None, help="覆盖 LLM provider")
    build.add_argument("--model", default=None, help="覆盖 LLM 模型名")

    run = sub.add_parser("run", help="运行现有单条款审查并生成预测")
    run.add_argument("--dataset", type=Path, default=defaults.dataset_path)
    run.add_argument("--cache-dir", type=Path, default=defaults.cache_dir)
    run.add_argument("--results-dir", type=Path, default=None)
    run.add_argument("--predictions", type=Path, default=None)
    run.add_argument("--party-stance", default="中立")
    run.add_argument("--concurrency", type=int, default=2)
    run.add_argument("--force", action="store_true", help="忽略已有预测缓存")

    metrics = sub.add_parser("metrics", help="基于 predictions.jsonl 重新计算指标")
    metrics.add_argument("--predictions", type=Path, required=True)
    metrics.add_argument("--results-dir", type=Path, required=True)

    experiment = sub.add_parser("experiment", help="运行 baseline LLM 与 system agent 对照实验")
    experiment.add_argument("--dataset", type=Path, default=defaults.dataset_path)
    experiment.add_argument("--results-root", type=Path, default=Path("eval/contract_clause_risk_review/results"))
    experiment.add_argument("--results-dir", type=Path, default=None, help="指定精确输出目录；默认自动按时间戳创建")
    experiment.add_argument("--run-name", default=None, help="可选实验名称；默认结果目录只使用时间戳")
    experiment.add_argument(
        "--sample-strategy",
        choices=["auto", "all", "stratified"],
        default="auto",
        help="抽样策略；auto 表示有 limit 时按 safe/medium/high 分层抽样，无 limit 时使用全量",
    )
    experiment.add_argument("--limit", type=int, default=None)
    experiment.add_argument("--seed", type=int, default=None, help="随机种子；默认使用当天日期 YYYYMMDD")
    experiment.add_argument("--party-stance", default="中立")
    experiment.add_argument("--provider", default=None)
    experiment.add_argument("--model", default=None)
    experiment.add_argument("--temperature", type=float, default=0.0)
    experiment.add_argument("--baseline-concurrency", type=int, default=4)
    experiment.add_argument("--system-concurrency", type=int, default=2)
    experiment.add_argument("--cache-dir", type=Path, default=defaults.cache_dir)
    experiment.add_argument("--trace-max-tool-artifact-chars", type=int, default=20000)
    experiment.add_argument("--no-progress", action="store_true", help="关闭命令行进度条")
    experiment.add_argument("--force", action="store_true", help="忽略已有 predictions/trace 和预测缓存")
    return parser


async def _cmd_build(args: argparse.Namespace) -> dict[str, object]:
    """执行 build 子命令。"""
    force_stages = _parse_force_stages(args.force)
    force_cache_tasks = _parse_force_cache_tasks(args.force_cache)
    config = BuildConfig(
        source_dir=args.source_dir,
        datasets_dir=args.datasets_dir,
        cache_dir=args.cache_dir,
        per_contract=args.per_contract,
        llm_prefilter_per_contract=args.llm_prefilter_per_contract,
        limit_contracts=args.limit_contracts,
        llm_filter=not args.no_llm_filter,
        generate_variants=not args.skip_generation,
        generation_attempts=args.generation_attempts,
        concurrency=args.concurrency,
        force_stages=force_stages,
    )
    cache = JsonFileCache(config.cache_dir)
    llm_client = LLMJsonClient(
        cache=cache,
        provider=args.provider,
        model=args.model,
        force_tasks=force_cache_tasks,
    )
    builder = DatasetBuilder(config, llm_client=llm_client)
    builder.cache = cache
    return await builder.build_all()


async def _cmd_run(args: argparse.Namespace) -> dict[str, object]:
    """执行 run 子命令。"""
    results_dir = args.results_dir or Path("eval/contract_clause_risk_review/results") / time.strftime("%Y%m%d-%H%M%S")
    ensure_dir(results_dir)
    predictions_path = args.predictions or (results_dir / "predictions.jsonl")
    cache = JsonFileCache(args.cache_dir)
    runner = ReviewRunner(
        cache=cache,
        party_stance=args.party_stance,
        concurrency=args.concurrency,
        force=args.force,
    )
    records = await runner.run_dataset(args.dataset, predictions_path)
    metrics = save_metrics_report(records, results_dir)
    run_manifest = {
        "dataset": str(args.dataset),
        "predictions": str(predictions_path),
        "results_dir": str(results_dir),
        "party_stance": args.party_stance,
        "concurrency": args.concurrency,
        "cache": cache.stats(),
        "metrics_summary": {
            "accuracy": metrics["accuracy"],
            "macro_f1": metrics["macro_f1"],
            "high_miss_rate": metrics["high_miss_rate"],
        },
    }
    write_json(results_dir / "run_manifest.json", run_manifest)
    return run_manifest


def _cmd_metrics(args: argparse.Namespace) -> dict[str, object]:
    """执行 metrics 子命令。"""
    records = load_predictions(args.predictions)
    return save_metrics_report(records, args.results_dir)


async def _cmd_experiment(args: argparse.Namespace) -> dict[str, object]:
    """执行 experiment 子命令。"""
    config = ExperimentConfig(
        dataset_path=args.dataset,
        results_root=args.results_root,
        result_dir=args.results_dir,
        run_name=args.run_name,
        sample_strategy=args.sample_strategy,
        limit=args.limit,
        seed=args.seed,
        party_stance=args.party_stance,
        provider=args.provider,
        model=args.model,
        temperature=args.temperature,
        baseline_concurrency=args.baseline_concurrency,
        system_concurrency=args.system_concurrency,
        cache_dir=args.cache_dir,
        force=args.force,
        trace_max_tool_artifact_chars=args.trace_max_tool_artifact_chars,
        show_progress=not args.no_progress,
    )
    return await run_experiment(config)


def _parse_force_stages(raw: str) -> set[str]:
    """解析强制重跑阶段参数。"""
    values = {item.strip() for item in (raw or "").split(",") if item.strip()}
    if "all" in values:
        return {"candidates", "select", "generate"}
    return values


def _parse_force_cache_tasks(raw: str) -> set[str]:
    """解析强制忽略 LLM 缓存的任务参数。"""
    values = {item.strip() for item in (raw or "").split(",") if item.strip()}
    if "all" in values:
        return {"clause_usefulness", "risk_injection", "risk_validation"}
    return values


if __name__ == "__main__":
    raise SystemExit(main())
