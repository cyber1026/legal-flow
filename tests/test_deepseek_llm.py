from __future__ import annotations

from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_core.messages import HumanMessage

from app.agents.law_tools import search_law, verify_law_article
from app.contracts.consistency_agent import submit_consistency_review
from app.contracts.review_agent import submit_review
from app.llm.deepseek import DeepSeekChatOpenAI


def _make_deepseek_model() -> DeepSeekChatOpenAI:
    return DeepSeekChatOpenAI(
        model="deepseek-v4-flash",
        api_key="test-key",
        base_url="https://api.deepseek.com/beta",
        streaming=True,
        deepseek_enable_thinking=True,
    )


def _assert_strict_object_schema(schema: dict) -> None:
    if schema.get("type") == "object":
        properties = schema.get("properties") or {}
        assert schema.get("additionalProperties") is False
        assert set(schema.get("required") or []) == set(properties)
        for child in properties.values():
            _assert_strict_object_schema(child)
    if schema.get("type") == "array":
        _assert_strict_object_schema(schema["items"])


def test_deepseek_review_payload_uses_thinking_strict_tools_without_tool_choice() -> None:
    model = _make_deepseek_model()

    bound = model.bind_tools(
        [verify_law_article, search_law, submit_review],
        strict=True,
    )
    payload = model._get_request_payload(
        [HumanMessage(content="hello")],
        **bound.kwargs,
    )

    assert "tool_choice" not in payload
    assert payload["extra_body"] == {"thinking": {"type": "enabled"}}
    assert {t["function"]["name"] for t in payload["tools"]} == {
        "verify_law_article",
        "search_law",
        "submit_review",
    }
    assert all(t["function"]["strict"] is True for t in payload["tools"])


def test_submit_review_tool_schema_is_deepseek_strict_compatible() -> None:
    schema = convert_to_openai_tool(submit_review, strict=True)
    parameters = schema["function"]["parameters"]

    assert "chunk_id" not in str(parameters)
    _assert_strict_object_schema(parameters)


def test_submit_consistency_review_tool_schema_is_deepseek_strict_compatible() -> None:
    # consistency agent 也走 DeepSeek strict：tool schema 必须每个对象 required 覆盖全部属性、
    # 且无「无属性 object」（evidence_facts 若是 list[dict] 会触发 strict 400，故必须是 list[str]）。
    schema = convert_to_openai_tool(submit_consistency_review, strict=True)
    _assert_strict_object_schema(schema["function"]["parameters"])
