from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import app.contracts.review_graph as rg
import app.contracts.review_pipeline as rp
from app.contracts.prompts.risk_schema import ClauseRiskAssessment, ReviewOpinion, ReviewOutput
from app.contracts.store import ClauseRecord, ContractRecord


def _contract(session_id: str | None = None) -> ContractRecord:
    return ContractRecord(
        id=10, user_id=1, session_id=session_id, job_id="job", filename="f.pdf",
        mime="application/pdf", doc_type="pdf", storage_path="/tmp/f.pdf",
        title="测试合同", status="pending", parsed_clauses=0, risk_count=0,
        error=None, started_at=None, finished_at=None,
        created_at=datetime(2026, 1, 1),
    )


def _clause(db_id: int, clause_id: str, clause_no: str, title: str) -> ClauseRecord:
    return ClauseRecord(
        db_id, 10, clause_id, "", clause_no, title, "条款正文",
        None, None, db_id - 1, "pending", False, [],
    )


def _risk_result() -> ReviewOutput:
    return ReviewOutput(
        has_opinion=True,
        opinions=[ReviewOpinion(
            opinion_type="警告", review_dimension="内容合法性",
            finding="风险说明", recommendation="改", confidence=0.8,
        )],
        risk_assessment=ClauseRiskAssessment(
            risk_level="high", rationale="条款存在高危安排", affected_party="甲方", confidence=0.8,
        ),
        consistency_facts=[],
        note="",
    )


def _setup(monkeypatch, clauses, events_fn, *, categories=None, contract=None, patch_overview=True):
    """打桩所有 I/O：ingest / 审查 agent / 分类 / 总览 / ContractStore。返回 inserted 列表。"""
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    def fake_run_config(**kwargs):
        """测试中不接入外部 tracing/callback。"""
        return {"run_name": kwargs.get("run_name", "test")}
    monkeypatch.setattr(rg, "build_run_config", fake_run_config)
    monkeypatch.setattr(rp, "build_run_config", fake_run_config)
    inserted: list[int] = []

    fake_ingest = SimpleNamespace(
        clause_records=clauses, parsed_doc=SimpleNamespace(title="测试合同")
    )
    monkeypatch.setattr(
        rg, "ContractIngestPipeline", lambda: SimpleNamespace(run=lambda cid: fake_ingest)
    )
    monkeypatch.setattr(rg, "areview_clause_events", events_fn)
    cat_map = categories or {c.clause_id: "其他" for c in clauses}
    async def fake_classify(cs, contract_id):
        return cat_map
    monkeypatch.setattr(rg, "_classify", fake_classify)
    async def fake_consistency_events(payload, run_config=None):
        """避免流水线单测触发真实一致性 LLM；模拟流式生成器，末尾 yield result。"""
        yield {"type": "think", "delta": "（一致性思考）"}
        yield {
            "type": "result",
            "review": SimpleNamespace(
                has_opinion=False,
                opinions=[],
                risk_assessment=SimpleNamespace(
                    risk_level="none",
                    rationale="未发现合同级一致性风险",
                    affected_party="不适用",
                    confidence=1.0,
                ),
                note="",
            ),
        }
    monkeypatch.setattr(rg, "areview_consistency_events", fake_consistency_events)
    if patch_overview:
        async def no_overview(state):
            return None
        monkeypatch.setattr(rg, "_generate_overview", no_overview)

    def fake_insert_opinion(**kwargs):
        inserted.append(kwargs["clause_db_id"])
        return SimpleNamespace(
            id=len(inserted), clause_id_ref=kwargs["clause_db_id"],
            opinion_type=kwargs["opinion_type"], review_dimension=kwargs["review_dimension"],
            finding=kwargs["finding"], recommendation=kwargs["recommendation"],
            confidence=kwargs["confidence"], citations=[],
        )

    rec = contract or _contract()
    monkeypatch.setattr(rg.ContractStore, "get_by_id", staticmethod(lambda cid: rec))
    monkeypatch.setattr(rg.ContractStore, "update_status", staticmethod(lambda cid, **kw: rec))
    monkeypatch.setattr(rg.ContractStore, "update_clause_review", staticmethod(lambda *a, **kw: None))
    monkeypatch.setattr(rg.ContractStore, "insert_review_opinion", staticmethod(fake_insert_opinion))
    monkeypatch.setattr(
        rg.ContractStore,
        "upsert_clause_risk_assessment",
        staticmethod(lambda **kw: SimpleNamespace(
            id=kw["clause_db_id"],
            contract_id=kw["contract_id"],
            clause_id_ref=kw["clause_db_id"],
            risk_level=kw["risk_level"],
            rationale=kw["rationale"],
            affected_party=kw["affected_party"],
            confidence=kw["confidence"],
            created_at=datetime(2026, 1, 1),
            to_dict=lambda: {
                "id": kw["clause_db_id"],
                "contract_id": kw["contract_id"],
                "clause_id_ref": kw["clause_db_id"],
                "risk_level": kw["risk_level"],
                "rationale": kw["rationale"],
                "affected_party": kw["affected_party"],
                "confidence": kw["confidence"],
                "created_at": "2026-01-01T00:00:00",
            },
        )),
    )
    monkeypatch.setattr(rg.ContractStore, "insert_consistency_fact", staticmethod(lambda **kw: None))
    monkeypatch.setattr(
        rg.ContractStore, "list_review_opinions",
        staticmethod(lambda cid: [
            SimpleNamespace(
                id=i,
                clause_id_ref=clause_db_id,
                opinion_type="警告",
                review_dimension="内容合法性",
                finding="风险说明",
                recommendation="改",
                confidence=0.8,
                citations=[],
            )
            for i, clause_db_id in enumerate(inserted, start=1)
        ]),
    )
    monkeypatch.setattr(
        rg.ContractStore, "list_clause_risk_assessments",
        staticmethod(lambda cid: [
            SimpleNamespace(
                id=clause_db_id,
                contract_id=10,
                clause_id_ref=clause_db_id,
                risk_level="high",
                rationale="条款存在高危安排",
                affected_party="甲方",
                confidence=0.8,
                created_at=datetime(2026, 1, 1),
                to_dict=lambda clause_db_id=clause_db_id: {
                    "id": clause_db_id,
                    "contract_id": 10,
                    "clause_id_ref": clause_db_id,
                    "risk_level": "high",
                    "rationale": "条款存在高危安排",
                    "affected_party": "甲方",
                    "confidence": 0.8,
                    "created_at": "2026-01-01T00:00:00",
                },
            )
            for clause_db_id in inserted
        ]),
    )
    monkeypatch.setattr(rg.ContractStore, "list_consistency_facts", staticmethod(lambda cid: []))
    monkeypatch.setattr(rg.ContractStore, "list_consistency_opinions", staticmethod(lambda cid: []))
    monkeypatch.setattr(rg.ContractStore, "get_consistency_risk_assessment", staticmethod(lambda cid: None))
    monkeypatch.setattr(
        rg.ContractStore,
        "upsert_consistency_risk_assessment",
        staticmethod(lambda **kw: SimpleNamespace(to_dict=lambda: kw)),
    )
    return inserted


def _drive(contract_id: int = 10):
    async def _run():
        return [ev async for ev in rp.astream_review_job(contract_id)]
    return asyncio.run(_run())


def test_astream_review_job_fans_out_and_persists(monkeypatch):
    clauses = [_clause(1, "c1", "1", "条款一"), _clause(2, "c2", "2", "条款二")]
    reviewed: list[str] = []

    async def fake_events(**kwargs):
        reviewed.append(kwargs["clause_no"])
        yield {"type": "think", "delta": "思考中"}
        yield {"type": "tool_start", "name": "verify_law_article", "args": {}, "call_id": "t1"}
        yield {"type": "tool_end", "name": "verify_law_article",
               "result_preview": "命中", "citations": [], "call_id": "t1"}
        yield {"type": "result", "review": _risk_result()}

    inserted = _setup(monkeypatch, clauses, fake_events)
    events = _drive()

    kinds = [e["event"] for e in events]
    assert sorted(reviewed) == ["1", "2"]
    assert sorted(inserted) == [1, 2]
    assert kinds[0] == "status"  # parsing
    assert kinds.count("clause_start") == 2
    assert kinds.count("clause_done") == 2
    done = [e for e in events if e["event"] == "done"]
    assert done and done[0]["data"]["risk_count"] == 2


def test_orphan_tool_start_is_closed_on_clause_done(monkeypatch):
    """工具只发 start 没发 end 时，后端应补 clause_tool_end，避免前端执行图永久 running。"""
    clauses = [_clause(1, "c1", "1", "条款一")]

    async def fake_events(**kwargs):
        yield {"type": "tool_start", "name": "search_playbook", "args": {}, "call_id": "t1"}
        yield {"type": "result", "review": ReviewOutput(
            has_opinion=False,
            opinions=[],
            risk_assessment=ClauseRiskAssessment(
                risk_level="none", rationale="无风险", affected_party="不适用", confidence=1.0,
            ),
            consistency_facts=[],
            note="",
        )}

    _setup(monkeypatch, clauses, fake_events)
    events = _drive()

    tool_end = [e for e in events if e["event"] == "clause_tool_end"]
    assert len(tool_end) == 1
    assert tool_end[0]["data"]["call_id"] == "t1"
    assert "未完成" in tool_end[0]["data"]["result_preview"]
    assert any(e["event"] == "clause_done" for e in events)


def test_single_clause_failure_is_isolated(monkeypatch):
    """单条款抛异常不应中断整图：其余条款照常完成，失败条款标 failed。"""
    clauses = [_clause(1, "c1", "1", "条款一"), _clause(2, "c2", "2", "条款二")]

    async def fake_events(**kwargs):
        if kwargs["clause_no"] == "2":
            raise ValueError("boom")
        yield {"type": "result", "review": _risk_result()}

    inserted = _setup(monkeypatch, clauses, fake_events)
    events = _drive()

    # c1 正常落库一条风险；c2 失败不落库。
    assert inserted == [1]
    done_events = [e for e in events if e["event"] == "clause_done"]
    assert len(done_events) == 2
    failed = [e for e in done_events if e["data"].get("failed")]
    assert len(failed) == 1 and failed[0]["data"]["clause_id"] == "c2"
    # 整图仍收口：done 事件带 failed_count=1。
    done = [e for e in events if e["event"] == "done"]
    assert done and done[0]["data"]["risk_count"] == 1
    assert done[0]["data"]["failed_count"] == 1


def test_skip_boilerplate_routes_around_review_agent(monkeypatch):
    """开启 skip 后，样板条款不进 review agent，直接 clause_done(skipped)。"""
    clauses = [_clause(1, "c1", "1", "核心"), _clause(2, "c2", "2", "标题页")]
    reviewed: list[str] = []

    async def fake_events(**kwargs):
        reviewed.append(kwargs["clause_no"])
        yield {"type": "result", "review": _risk_result()}

    _setup(
        monkeypatch, clauses, fake_events,
        categories={"c1": "核心义务", "c2": "样板条款"},
    )
    monkeypatch.setattr(rg.settings, "review_skip_boilerplate", True)
    events = _drive()

    assert reviewed == ["1"]  # 只有非样板条款被送审
    skipped = [
        e for e in events
        if e["event"] == "clause_done" and e["data"].get("skipped")
    ]
    assert len(skipped) == 1 and skipped[0]["data"]["clause_id"] == "c2"


def test_all_skipped_still_completes(monkeypatch):
    """全部条款被判样板而跳过时，空 fan-out 仍须经 aggregate 收口并发 done。"""
    clauses = [_clause(1, "c1", "1", "标题"), _clause(2, "c2", "2", "签署页")]
    reviewed: list[str] = []

    async def fake_events(**kwargs):
        reviewed.append(kwargs["clause_no"])
        yield {"type": "result", "review": _risk_result()}

    _setup(
        monkeypatch, clauses, fake_events,
        categories={"c1": "样板条款", "c2": "样板条款"},
    )
    monkeypatch.setattr(rg.settings, "review_skip_boilerplate", True)
    events = _drive()

    assert reviewed == []  # review agent 完全没被调用
    kinds = [e["event"] for e in events]
    assert kinds.count("clause_done") == 2  # 两条都发了 skipped 的 clause_done
    done = [e for e in events if e["event"] == "done"]
    assert done and done[0]["data"]["risk_count"] == 0


def test_overview_streams_and_persists(monkeypatch):
    """_generate_overview 在 chat 线程上经顶层图生成总览：流式事件 + 落库 + 传对 thread_id。"""
    events: list[dict] = []
    appended: list[tuple] = []
    captured: dict = {}

    async def fake_astream_events(payload, version=None, config=None, subgraphs=None):
        captured["config"] = config
        captured["subgraphs"] = subgraphs
        chunk = SimpleNamespace(
            content="## 总览\n未发现显著风险",
            tool_call_chunks=None, tool_calls=None, additional_kwargs={},
        )
        yield {"event": "on_chat_model_stream", "data": {"chunk": chunk}}

    monkeypatch.setattr(
        rg, "get_supervisor_agent",
        lambda: SimpleNamespace(astream_events=fake_astream_events),
    )
    monkeypatch.setattr(
        rg.SessionStore, "append_message",
        staticmethod(lambda *a, **kw: appended.append((a, kw))),
    )
    async def fake_to_thread(func, /, *args, **kwargs):
        """测试中直接执行已打桩的同步函数，避免启动线程池。"""
        return func(*args, **kwargs)
    monkeypatch.setattr(rg.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(rg, "get_stream_writer", lambda: events.append)

    asyncio.run(rg._generate_overview({
        "contract_id": 10,
        "contract_title": "测试合同",
        "session_id": "s1",
        "party_stance": "未知",
        "contract_clauses": [],
        "clause_categories": {},
        "findings": [],
        "failed_clauses": [],
        "consistency_review": {},
        "risk_count": 0,
        "final_report": {},
    }))
    kinds = [e["event"] for e in events]
    assert "overview_start" in kinds
    assert any(e["event"] == "overview_delta" for e in events)
    assert "overview_done" in kinds
    assert len(appended) == 1
    args, _kw = appended[0]
    assert args[0] == "s1" and args[1] == "assistant"
    assert "总览" in args[2]
    assert captured["config"]["configurable"]["thread_id"] == "s1"
    assert captured["subgraphs"] is True
