"""合同条款风险评测工具包。

包含标准合同条款抽取、风险样本构建、审查运行器和指标计算。
"""

from eval.contract_clause_risk_review.schemas import EvalLabel, SYSTEM_TO_EVAL_LABEL

__all__ = ["EvalLabel", "SYSTEM_TO_EVAL_LABEL"]
