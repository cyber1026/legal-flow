# 合同条款风险评测

这个目录提供合同单条款风险识别评测工具，用来观察系统在 `safe` / `medium` / `high` 三档风险标签上的表现，尤其关注高风险漏检率。

## 数据集

数据集文件路径如下：

| 文件 | 说明 |
| --- | --- |
| `contract_risk_single_clause.jsonl` | 主评测集 |
| `contract_risk_high_recall.jsonl` | 高风险召回评测子集 |
| `selected_safe_clauses.jsonl` | 安全条款 seed |
| `candidate_clauses.jsonl` | 候选条款池 |
| `rejected_generated_samples.jsonl` | 被拒绝的生成样本，供人工复核口径 |
| `contract_risk_manifest.json` | 构建参数、样本分布、缺口合同和标签映射 |

运行缓存和实验结果默认写入：

```text
eval/contract_clause_risk_review/cache/
eval/contract_clause_risk_review/results/
```

## CLI

```bash
uv run python -m eval.contract_clause_risk_review --help
```

使用自有语料构建本地数据集：

```bash
uv run python -m eval.contract_clause_risk_review build
```

运行系统审查：

```bash
uv run python -m eval.contract_clause_risk_review run \
  --dataset eval/contract_clause_risk_review/datasets/contract_risk_single_clause.jsonl \
  --party-stance 中立
```

运行 baseline 与 system agent 对照实验：

```bash
uv run python -m eval.contract_clause_risk_review experiment \
  --dataset eval/contract_clause_risk_review/datasets/contract_risk_single_clause.jsonl \
  --limit 30 \
  --party-stance 中立
```

基于已有预测重新计算指标：

```bash
uv run python -m eval.contract_clause_risk_review metrics \
  --predictions eval/contract_clause_risk_review/results/<run>/predictions.jsonl \
  --results-dir eval/contract_clause_risk_review/results/<run>
```

## 指标口径

- `accuracy`：三档标签准确率。
- `macro_f1`：三档宏平均 F1。
- `high_miss_rate`：真实高风险样本中未被识别为高风险的比例。

标签映射以本地生成的 `contract_risk_manifest.json` 为准：系统侧 `critical/high` 映射为 `high`，`medium` 保持不变，`low/none` 映射为 `safe`。
