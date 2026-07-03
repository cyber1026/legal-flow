from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.contracts.prompts.risk_schema import ClauseRiskAssessment
from app.db import _SCHEMA_STATEMENTS


def test_risk_level_schema_accepts_lowercase():
    item = ClauseRiskAssessment(
        risk_level="high",
        rationale="条款存在明显不利安排。",
        affected_party="甲方",
        confidence=0.8,
    )

    assert item.risk_level == "high"


def test_risk_level_schema_rejects_legacy_uppercase():
    with pytest.raises(ValidationError):
        ClauseRiskAssessment(
            risk_level="High",
            rationale="条款存在明显不利安排。",
            affected_party="甲方",
            confidence=0.8,
        )


def test_schema_contains_legacy_risk_level_migration_and_unique_indexes():
    statements = list(_SCHEMA_STATEMENTS)
    joined = "\n".join(statements)

    drop_idx = next(
        i for i, stmt in enumerate(statements)
        if "DROP CONSTRAINT IF EXISTS risk_items_risk_level_check" in stmt
    )
    migrate_idx = next(
        i for i, stmt in enumerate(statements)
        if "SET risk_level = LOWER(risk_level)" in stmt
    )
    check_idx = next(
        i for i, stmt in enumerate(statements)
        if "CHECK (risk_level IN ('none','low','medium','high','critical'))" in stmt
        and "ADD CONSTRAINT" in stmt
    )

    assert drop_idx < migrate_idx < check_idx
    assert "CREATE TABLE IF NOT EXISTS law_ingest_jobs" in joined
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_contract_clauses_contract_clause" in joined
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_risk_citations_risk_article" in joined
    assert "CREATE TABLE IF NOT EXISTS review_opinions" in joined
    assert "CREATE TABLE IF NOT EXISTS clause_risk_assessments" in joined
    assert "CREATE TABLE IF NOT EXISTS contract_consistency_opinions" in joined
