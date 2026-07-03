"""审查支撑库「检索 + 总结」工具的单测：空召回短路 / 条款上下文抽取 / digest+artifact / 降级。"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

from langchain_core.messages import HumanMessage

import app.knowledge.summarize as ks
from app.knowledge.registry import KB_BY_KEY


class _RecordLLM:
    def __init__(self):
        self.calls = []

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        return SimpleNamespace(content="要点：…")


def _patch(monkeypatch, results, llm):
    """打桩 KBRetriever（返回固定 results）与总结快模型。"""

    class _FakeRetriever:
        def __init__(self, collection):
            pass

        def search(self, query, *, contract_domain="", clause_type=""):
            return results

    monkeypatch.setattr(ks, "KBRetriever", _FakeRetriever)
    monkeypatch.setattr(ks, "_get_summarize_llm", lambda: llm)


def _result(cid="c1"):
    return {
        "chunk_id": cid,
        "display_text": "司法解释正文，关于违约金调整……" * 30,  # 故意超长，验证 artifact 截断
        "embedding_text": "向量文本",
        "score": 0.7,
        "source_url": "http://example.com/1",
        "contract_domains": ["买卖/供货"],
        "clause_types": ["违约"],
    }


class _BoomLLM:
    def invoke(self, messages, **kwargs):
        raise AssertionError("空召回时不应调用总结模型")


def test_空召回短路不调模型(monkeypatch):
    _patch(monkeypatch, [], _BoomLLM())
    tool = ks.build_kb_tool(KB_BY_KEY["judicial"])

    content, artifact = tool.func(query="违约金过高", state={"messages": []})

    assert "未检索到" in content
    assert artifact["kb"] == "judicial"
    assert artifact["results"] == []


def test_总结抽取条款上下文并返回digest(monkeypatch):
    class _RecordLLM:
        def __init__(self):
            self.calls = []

        def invoke(self, messages, **kwargs):
            self.calls.append(messages)
            return SimpleNamespace(content="要点：本条违约金或可调整 (来源: http://example.com/1)")

    llm = _RecordLLM()
    _patch(monkeypatch, [_result()], llm)
    tool = ks.build_kb_tool(KB_BY_KEY["judicial"])

    state = {
        "messages": [
            HumanMessage(content="合同：测试合同\n\n请审查以下合同条款：\n```\n本条违约金为合同总额的50%\n```")
        ]
    }
    content, artifact = tool.func(query="违约金过高", state=state, clause_type="违约")

    assert content.startswith("要点：")
    # 条款正文应被抽取并拼进 human prompt（messages[1] = HumanMessage）
    human = llm.calls[0][1]
    assert "本条违约金为合同总额的50%" in human.content
    assert "违约金过高" in human.content                  # query 也在
    # artifact：裁剪后的召回，excerpt 截断到 500
    assert artifact["kb"] == "judicial"
    assert artifact["results"][0]["chunk_id"] == "c1"
    assert len(artifact["results"][0]["excerpt"]) <= 500


def test_案例结果来源优先案件名称和日期(monkeypatch):
    llm = _RecordLLM()
    result = {
        "kb_type": "case_rule",
        "chunk_id": "case1",
        "title": "陈某芬诉罗某、何某琴民间借贷纠纷案",
        "citation": "（2023）沪02民再21号·民间借贷纠纷",
        "publish_time": "2023.06.21",
        "display_text": "夫妻共同债务裁判规则",
        "score": 0.8,
        "source_url": "http://example.com/case",
        "contract_domains": ["借贷/金融"],
        "clause_types": ["债务承担"],
    }
    _patch(monkeypatch, [result], llm)
    tool = ks.build_kb_tool(KB_BY_KEY["case"])

    content, artifact = tool.func(query="夫妻共同债务", state={"messages": []})

    assert content == "要点：…"
    human = llm.calls[0][1]
    assert "[1] 陈某芬诉罗某、何某琴民间借贷纠纷案" in human.content
    assert "来源 陈某芬诉罗某、何某琴民间借贷纠纷案（2023.06.21）" in human.content
    assert "来源 http://example.com/case" not in human.content
    assert artifact["results"][0]["label"] == "陈某芬诉罗某、何某琴民间借贷纠纷案"


def test_案例总结要求更完整类案上下文(monkeypatch):
    llm = _RecordLLM()
    long_facts = "基本案情：" + "甲向乙借款用于家庭共同生活。" * 70
    result = {
        "kb_type": "case_rule",
        "chunk_id": "case1",
        "title": "陈某芬诉罗某、何某琴民间借贷纠纷案",
        "citation": "（2023）沪02民再21号·民间借贷纠纷",
        "publish_time": "2023.06.21",
        "relevant_statutes": "《中华人民共和国民法典》第1064条第2款",
        "cites": [{"law": "民法典", "article": "第1064条第2款"}],
        "display_text": (
            "裁判要旨：经营收入用于家庭共同生活的，可以认定为夫妻共同债务。\n"
            + long_facts
            + "\n裁判理由：借款虽超出日常生活需要，但经营收益用于购房、子女学费。\n"
            + "裁判结果：夫妻双方共同偿还。"
        ),
        "score": 0.8,
        "source_url": "http://example.com/case",
        "contract_domains": ["金融担保"],
        "clause_types": ["债务承担"],
    }
    _patch(monkeypatch, [result], llm)
    tool = ks.build_kb_tool(KB_BY_KEY["case"])

    tool.func(query="夫妻共同债务", state={"messages": []})

    system = llm.calls[0][0].content
    human = llm.calls[0][1].content
    assert "相关法条" in system
    assert "裁判要旨" in system
    assert "基本案情" in system
    assert "裁判理由或裁判结果" in system
    assert "必须完整列出" in system
    assert "相关法条原文：《中华人民共和国民法典》第1064条第2款" in human
    assert "裁判结果：夫妻双方共同偿还" in human


def test_司法解释总结不把具体情形误判为未检索到(monkeypatch):
    llm = _RecordLLM()
    result = {
        "kb_type": "judicial_article",
        "chunk_id": "ji1#a01",
        "title": "建设工程施工合同解释（一）",
        "article_no": "第一条",
        "citation": "建设工程施工合同解释（一） 第一条",
        "display_text": (
            "建设工程施工合同解释（一） 第一条\n"
            "建设工程施工合同具有下列情形之一的，应当依据民法典第一百五十三条第一款的规定，认定无效：\n"
            "（一）承包人未取得建筑业企业资质或者超越资质等级的；\n"
            "（二）没有资质的实际施工人借用有资质的建筑施工企业名义的；\n"
            "（三）建设工程必须进行招标而未招标或者中标无效的。\n"
            "承包人因转包、违法分包建设工程与他人签订的建设工程施工合同，应当认定无效。"
        ),
        "score": 0.78,
        "source_url": "http://example.com/ji",
        "contract_domains": ["建设工程"],
        "clause_types": ["主体", "解除终止"],
    }
    _patch(monkeypatch, [result], llm)
    tool = ks.build_kb_tool(KB_BY_KEY["judicial"])

    tool.func(query="建设工程施工合同无效情形", state={"messages": []})

    system = llm.calls[0][0].content
    human = llm.calls[0][1].content
    assert "具体情形、构成要件、认定标准、法律后果或法院口径" in system
    assert "不得因此说「未检索到」" in system
    assert "建设工程施工合同解释（一） 第一条" in human
    assert "承包人未取得建筑业企业资质或者超越资质等级" in human


def test_司法解释来源展示正式标题而不是简称(monkeypatch):
    llm = _RecordLLM()
    result = {
        "kb_type": "judicial_article",
        "chunk_id": "ji1#a19",
        "title": "买卖合同解释",
        "source_title": "最高人民法院关于审理买卖合同纠纷案件适用法律问题的解释",
        "article_no": "第十九条",
        "citation": "买卖合同解释 第十九条",
        "display_text": "买卖合同解释 第十九条\n买受人违约造成损失的处理口径。",
        "score": 0.78,
        "source_url": "http://example.com/ji",
        "contract_domains": ["买卖/供货"],
        "clause_types": ["违约"],
    }
    _patch(monkeypatch, [result], llm)
    tool = ks.build_kb_tool(KB_BY_KEY["judicial"])

    tool.func(query="买卖合同纠纷 裁判规则", state={"messages": []})

    human = llm.calls[0][1].content
    assert "最高人民法院关于审理买卖合同纠纷案件适用法律问题的解释 第十九条" in human
    assert "来源 最高人民法院关于审理买卖合同纠纷案件适用法律问题的解释 第十九条" in human


def test_模型失败降级为原始召回(monkeypatch):
    class _ErrLLM:
        def invoke(self, messages, **kwargs):
            raise RuntimeError("模型不可用")

    _patch(monkeypatch, [_result("cX")], _ErrLLM())
    tool = ks.build_kb_tool(KB_BY_KEY["case"])

    content, artifact = tool.func(query="类案", state={"messages": []})

    assert "自动摘要暂不可用" in content                 # 降级提示
    assert artifact["results"][0]["chunk_id"] == "cX"   # 仍带原始召回


def test_检索超时快速返回并带tool结果(monkeypatch):
    class _SlowRetriever:
        def __init__(self, collection):
            pass

        def search(self, query, *, contract_domain="", clause_type=""):
            time.sleep(0.2)
            return [_result()]

    monkeypatch.setattr(ks, "KBRetriever", _SlowRetriever)
    monkeypatch.setattr(ks.settings, "kb_retrieve_timeout", 0.01)
    tool = ks.build_kb_tool(KB_BY_KEY["playbook"])

    started = time.perf_counter()
    content, artifact = tool.func(query="不可抗力条款", state={"messages": []})

    assert time.perf_counter() - started < 0.15
    assert "检索超时" in content
    assert artifact["error"] == "timeout"
    assert artifact["results"] == []


def test_总结超时降级为原始召回(monkeypatch):
    class _SlowLLM:
        def invoke(self, messages, **kwargs):
            time.sleep(0.2)
            return SimpleNamespace(content="太晚了")

    _patch(monkeypatch, [_result("cY")], _SlowLLM())
    monkeypatch.setattr(ks.settings, "kb_summarize_timeout", 0.01)
    tool = ks.build_kb_tool(KB_BY_KEY["playbook"])

    started = time.perf_counter()
    content, artifact = tool.func(query="不可抗力条款", state={"messages": []})

    assert time.perf_counter() - started < 0.15
    assert "自动摘要暂不可用" in content
    assert artifact["results"][0]["chunk_id"] == "cY"


def test_空query直接提示(monkeypatch):
    _patch(monkeypatch, [_result()], _BoomLLM())
    tool = ks.build_kb_tool(KB_BY_KEY["playbook"])
    content, artifact = tool.func(query="   ", state={"messages": []})
    assert "请提供检索意图" in content
    assert artifact["results"] == []


def test_无条款上下文时仍按query总结(monkeypatch):
    """supervisor/普通法律问答(无待审条款)：检索有结果时应调用模型按 query 总结，而非过滤为空。

    复现并锁定 bug：此前 prompt 硬绑「当前待审条款」，无条款时模型一律回「未发现…」，
    导致问「合同法司法解释」检索不到结果。"""
    llm = _RecordLLM()
    _patch(monkeypatch, [_result()], llm)
    tool = ks.build_kb_tool(KB_BY_KEY["judicial"])

    content, artifact = tool.func(query="合同法司法解释", state={"messages": []})

    assert content == "要点：…"          # 走了总结，没有被过滤掉
    assert len(llm.calls) == 1           # 确实调用了模型
    human = llm.calls[0][1]
    assert "合同法司法解释" in human.content       # query 进了 prompt
    assert "【当前待审条款】" not in human.content  # 无条款时不带条款段


def test_render_field_把outline的JSON渲染成行():
    s = json.dumps(["第一条 标的", "第二条 价款"], ensure_ascii=False)
    assert ks._render_field(s) == "第一条 标的\n第二条 价款"
    assert ks._render_field("普通文本") == "普通文本"


def test_标准合同总结基于完整结构outline(monkeypatch):
    """standard_contract 的 content_fields=(contract_outline, display_text)：
    总结时应把完整条款结构清单喂给模型，而非仅截断正文。"""
    llm = _RecordLLM()
    results = [
        {
            "chunk_id": "k1",
            "contract_title": "测试买卖合同",
            "contract_outline": json.dumps(["第一条 标的", "第二条 价款", "第三条 违约责任"], ensure_ascii=False),
            "display_text": "合同正文摘录……",
            "score": 0.6,
            "contract_domains": ["买卖/供货"],
            "clause_types": ["标的"],
        }
    ]
    _patch(monkeypatch, results, llm)
    tool = ks.build_kb_tool(KB_BY_KEY["standard_contract"])
    content, artifact = tool.func(query="买卖合同应包含哪些条款", state={"messages": []})

    human = llm.calls[0][1]
    assert "第一条 标的" in human.content
    assert "第三条 违约责任" in human.content      # 完整结构清单进了 prompt
    assert "合同正文摘录" in human.content          # 正文摘录也在
