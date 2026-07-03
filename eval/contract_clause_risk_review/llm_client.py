"""带 JSON 解析与文件缓存的 LLM 客户端。"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage

from app.core.config import settings
from app.llm.factory import get_chat_llm
from eval.contract_clause_risk_review.cache import JsonFileCache
from eval.contract_clause_risk_review.prompts import render_prompt

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class LLMJsonClient:
    """封装项目 LLM，要求模型返回 JSON，并把结果写入缓存。"""

    def __init__(
        self,
        *,
        cache: JsonFileCache,
        provider: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,
        force_tasks: set[str] | None = None,
        max_attempts: int = 3,
    ) -> None:
        """初始化 LLM JSON 客户端。"""
        self.cache = cache
        self.provider = provider or settings.llm_provider
        self.model = model or self._default_model_name(self.provider)
        self.temperature = temperature
        self.force_tasks = force_tasks or set()
        self.max_attempts = max(1, max_attempts)

    def model_identity(self) -> str:
        """返回用于缓存 key 的模型身份。"""
        mode = "json_no_thinking" if self.provider.lower() in {"deepseek", "zhipuai"} else "json"
        return f"{self.provider}:{self.model}:temperature={self.temperature}:{mode}"

    def _default_model_name(self, provider: str) -> str:
        """根据 provider 读取项目默认模型名。"""
        provider = provider.lower()
        if provider == "deepseek":
            return settings.deepseek_model
        if provider == "gemini":
            return settings.google_model
        if provider == "zhipuai":
            return settings.zhipuai_model
        return "default"

    async def complete_json(
        self,
        *,
        task: str,
        prompt: str,
        prompt_hash: str,
        input_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """调用 LLM 生成 JSON，并按任务缓存结果。"""
        key = self.cache.build_key(
            task=task,
            prompt_hash=prompt_hash,
            input_payload=input_payload,
            model=self.model_identity(),
        )
        if task not in self.force_tasks:
            cached = self.cache.get(task, key)
            if cached is not None:
                output = cached.get("output")
                if isinstance(output, dict):
                    return output

        llm_kwargs: dict[str, Any] = {
            "provider": self.provider,
            "model": self.model,
            "temperature": self.temperature,
            "timeout": settings.llm_review_timeout,
        }
        if self.provider.lower() in {"deepseek", "zhipuai"}:
            llm_kwargs["enable_thinking"] = False
        llm = get_chat_llm(**llm_kwargs)
        base_message = render_prompt(prompt, input_payload)
        last_error: Exception | None = None
        parsed: dict[str, Any] | None = None
        for attempt in range(1, self.max_attempts + 1):
            message = base_message
            if attempt > 1:
                message += (
                    "\n\n上一次响应不是合法 JSON。请严格只输出一个 JSON 对象，"
                    "不要解释、不要 Markdown、不要代码块外文本。"
                )
            response = await llm.ainvoke([HumanMessage(content=message)])
            content = getattr(response, "content", "")
            try:
                parsed = parse_json_response(content)
                break
            except Exception as exc:
                last_error = exc
        if parsed is None:
            raise ValueError(f"LLM JSON 解析失败，已重试 {self.max_attempts} 次：{last_error}")
        self.cache.set(
            task,
            key,
            model=self.model_identity(),
            prompt_hash=prompt_hash,
            input_payload=input_payload,
            output=parsed,
        )
        return parsed


def parse_json_response(content: Any) -> dict[str, Any]:
    """从模型响应中解析 JSON 对象。"""
    if isinstance(content, list):
        text = "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    else:
        text = str(content or "")
    text = text.strip()
    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("LLM 返回的 JSON 顶层必须是对象")
    return value
