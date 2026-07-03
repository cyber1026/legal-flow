"""合同风险评测构建使用的 LLM prompt。"""

from __future__ import annotations

import hashlib

from eval.contract_clause_risk_review.io_utils import stable_json_dumps

CLAUSE_USEFULNESS_PROMPT = """你是合同风险评测数据集构建助手。请判断给定标准合同条款是否适合作为“单条款风险识别”评测 seed。

评测 seed 应该承载可审查的权利义务、责任、期限、价款、交付、验收、质量、解除、争议解决、信息合规、资质许可等风险点。
不要选择纯定义、纯分类、纯说明、签章页、表格占位、附件空白、缺少实质权利义务的条款。

请只输出 JSON，不要 Markdown。格式：
{
  "decision": "useful" 或 "not_useful",
  "risk_dimensions": ["付款价款" 等],
  "reason": "一句话说明",
  "confidence": 0.0 到 1.0
}
"""

RISK_INJECTION_PROMPT = """你是合同风险评测数据集构建助手。请基于给定标准合同条款，生成一个目标风险等级的风险注入变体。

风险标签为三档：
- safe：标准合同中的无风险/低风险条款。
- medium：存在明确不利或不完整安排，但通常可通过补充约定、谈判或提示控制。
- high：可能导致重大权利丧失、责任严重失衡、无效/不可执行、重大损失或关键救济缺失。

要求：
1. 保留原条款主题和合同语境，不要改成完全无关的新条款。
2. 只注入一个主风险模式，避免多个风险混杂导致标签不清。
3. 输出条款必须像合同条款，不要加入评测解释。
4. 目标为 high 时风险必须足够明显；目标为 medium 时不要过度升级为 high。

请只输出 JSON，不要 Markdown。格式：
{
  "injected_clause": "修改后的完整条款文本",
  "target_label": "medium 或 high",
  "risk_pattern": "风险模式短语",
  "changes": ["修改点1", "修改点2"],
  "expected_issue": "模型应该识别出的核心风险",
  "label_reason": "为什么是该等级"
}
"""

RISK_VALIDATION_PROMPT = """你是合同风险评测标签复核助手。请判断给定风险注入条款的真实风险等级是否符合目标标签。

风险标签为三档：
- safe：无风险/低风险。
- medium：存在明确不利或不完整安排，但通常可通过补充约定、谈判或提示控制。
- high：可能导致重大权利丧失、责任严重失衡、无效/不可执行、重大损失或关键救济缺失。

请按中立立场判断，不站在甲方或乙方任一方放大风险。

请只输出 JSON，不要 Markdown。格式：
{
  "accepted": true 或 false,
  "label": "safe、medium 或 high",
  "reason": "一句话说明",
  "confidence": 0.0 到 1.0
}
"""


def prompt_hash(prompt: str) -> str:
    """计算 prompt 的稳定哈希。"""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def build_clause_usefulness_input(
    *,
    contract_name: str,
    clause_no: str,
    title: str,
    section_path: str,
    text: str,
    rule_dimensions: list[str],
) -> dict[str, object]:
    """构造条款有效性判断输入。"""
    return {
        "contract_name": contract_name,
        "clause_no": clause_no,
        "title": title,
        "section_path": section_path,
        "rule_dimensions": rule_dimensions,
        "clause_text": text,
    }


def build_risk_injection_input(
    *,
    target_label: str,
    contract_name: str,
    clause_no: str,
    title: str,
    text: str,
    attempt: int = 1,
    retry_note: str = "",
) -> dict[str, object]:
    """构造风险注入输入。"""
    payload: dict[str, object] = {
        "target_label": target_label,
        "contract_name": contract_name,
        "clause_no": clause_no,
        "title": title,
        "standard_clause": text,
    }
    if attempt > 1:
        payload["attempt"] = attempt
    if retry_note:
        payload["retry_note"] = retry_note
    return payload


def build_validation_input(
    *,
    target_label: str,
    injected_clause: str,
    risk_pattern: str,
    expected_issue: str,
) -> dict[str, object]:
    """构造风险注入标签复核输入。"""
    return {
        "target_label": target_label,
        "injected_clause": injected_clause,
        "risk_pattern": risk_pattern,
        "expected_issue": expected_issue,
    }


def render_prompt(prompt: str, payload: dict[str, object]) -> str:
    """把系统说明和结构化输入渲染为单条用户消息。"""
    return f"{prompt}\n\n输入：\n{stable_json_dumps(payload)}"
