"""Shared PostgreSQL connection helpers.

业务关系数据（用户、会话、消息）统一走 PostgreSQL。
RAG 响应延迟主要由 LLM 调用主导，所以这里使用同步的 psycopg3 +
连接池就够用了，没必要引入 async 层。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.core.config import settings

# 进程内单例连接池；首次使用时按需创建（避免在 import 阶段就尝试连库）。
_pool: ConnectionPool | None = None


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    """从连接池借一条连接；正常退出自动 commit，异常自动 rollback。"""
    pool = _get_pool()
    with pool.connection() as conn:
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise


# 表结构 DDL。每条语句独立执行，全部使用 IF NOT EXISTS，整体幂等。
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id            BIGSERIAL PRIMARY KEY,
        email         TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id         TEXT PRIMARY KEY,
        user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title      TEXT NOT NULL DEFAULT '新会话',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id)",
    """
    CREATE TABLE IF NOT EXISTS messages (
        id              BIGSERIAL PRIMARY KEY,
        session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        role            TEXT NOT NULL CHECK (role IN ('user','assistant','system','tool')),
        content         TEXT NOT NULL,
        citations_json  TEXT,
        tool_calls_json TEXT,
        thinking        TEXT,
        thinking_ms     INTEGER,
        reasoning_json  TEXT,
        images_json     TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # 既有库补列：按真实顺序记录「思考↔工具」交替时间线（ReAct 过程还原）
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS reasoning_json TEXT",
    "CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id)",
    # ──────────────────────────────────────────────────────────────────
    # 法律文档入库任务
    # ──────────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS law_ingest_jobs (
        job_id          TEXT PRIMARY KEY,
        user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        filename        TEXT NOT NULL,
        law_name        TEXT,
        status          TEXT NOT NULL
            CHECK (status IN ('pending','parsing','embedding','done','failed')),
        parsed_chunks   INTEGER,
        embedded_chunks INTEGER,
        error           TEXT,
        started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finished_at     TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_law_ingest_jobs_user_id ON law_ingest_jobs(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_law_ingest_jobs_started_at ON law_ingest_jobs(started_at DESC)",
    # ──────────────────────────────────────────────────────────────────
    # 合同智能审查相关表
    # ──────────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS contracts (
        id              BIGSERIAL PRIMARY KEY,
        user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        session_id      TEXT REFERENCES sessions(id) ON DELETE CASCADE,
        job_id          TEXT NOT NULL UNIQUE,
        filename        TEXT NOT NULL,
        mime            TEXT NOT NULL DEFAULT '',
        doc_type        TEXT NOT NULL CHECK (doc_type IN ('image','pdf','docx')),
        storage_path    TEXT NOT NULL,
        title           TEXT NOT NULL DEFAULT '',
        status          TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','parsing','embedding','reviewing','done','failed')),
        parsed_clauses  INTEGER NOT NULL DEFAULT 0,
        risk_count      INTEGER NOT NULL DEFAULT 0,
        error           TEXT,
        started_at      TIMESTAMPTZ,
        finished_at     TIMESTAMPTZ,
        party_stance    TEXT NOT NULL DEFAULT '未知'
            CHECK (party_stance IN ('甲方','乙方','中立','未知')),
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # 旧库 ALTER：合同从属会话（合同审查作为会话内可开关模块）
    """
    ALTER TABLE contracts ADD COLUMN IF NOT EXISTS session_id TEXT
        REFERENCES sessions(id) ON DELETE CASCADE
    """,
    # 旧库 ALTER：委托人立场（为立场 HITL 预留；甲方/乙方/中立/未知）
    """
    ALTER TABLE contracts ADD COLUMN IF NOT EXISTS party_stance TEXT NOT NULL DEFAULT '未知'
    """,
    "ALTER TABLE contracts DROP CONSTRAINT IF EXISTS contracts_party_stance_check",
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'contracts_party_stance_check'
              AND conrelid = 'contracts'::regclass
        ) THEN
            ALTER TABLE contracts
            ADD CONSTRAINT contracts_party_stance_check
            CHECK (party_stance IN ('甲方','乙方','中立','未知'));
        END IF;
    END $$;
    """,
    "CREATE INDEX IF NOT EXISTS idx_contracts_user_id ON contracts(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_contracts_session_id ON contracts(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_contracts_job_id ON contracts(job_id)",
    """
    CREATE TABLE IF NOT EXISTS contract_clauses (
        id              BIGSERIAL PRIMARY KEY,
        contract_id     BIGINT NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
        clause_id       TEXT NOT NULL,
        section_path    TEXT NOT NULL DEFAULT '',
        clause_no       TEXT NOT NULL DEFAULT '',
        title           TEXT NOT NULL DEFAULT '',
        text            TEXT NOT NULL,
        page_no         INTEGER,
        bbox_json       TEXT,
        chunk_index     INTEGER NOT NULL DEFAULT 0,
        review_status   TEXT NOT NULL DEFAULT 'pending'
            CHECK (review_status IN ('pending','reviewing','done','skipped','failed')),
        review_has_risk BOOLEAN NOT NULL DEFAULT false,
        reasoning_json  TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "ALTER TABLE contract_clauses ADD COLUMN IF NOT EXISTS review_status TEXT NOT NULL DEFAULT 'pending'",
    "ALTER TABLE contract_clauses ADD COLUMN IF NOT EXISTS review_has_risk BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE contract_clauses ADD COLUMN IF NOT EXISTS reasoning_json TEXT",
    "ALTER TABLE contract_clauses DROP CONSTRAINT IF EXISTS contract_clauses_review_status_check",
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conname = 'contract_clauses_review_status_check'
              AND conrelid = 'contract_clauses'::regclass
        ) THEN
            ALTER TABLE contract_clauses
            ADD CONSTRAINT contract_clauses_review_status_check
            CHECK (review_status IN ('pending','reviewing','done','skipped','failed'));
        END IF;
    END $$;
    """,
    "CREATE INDEX IF NOT EXISTS idx_clauses_contract_id ON contract_clauses(contract_id)",
    """
    CREATE TABLE IF NOT EXISTS risk_items (
        id              BIGSERIAL PRIMARY KEY,
        contract_id     BIGINT NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
        clause_id_ref   BIGINT NOT NULL REFERENCES contract_clauses(id) ON DELETE CASCADE,
        risk_type       TEXT,
        opinion_type    TEXT NOT NULL DEFAULT '提醒',
        review_dimension TEXT NOT NULL DEFAULT '内容合法性',
        risk_level      TEXT NOT NULL CHECK (risk_level IN ('none','low','medium','high','critical')),
        description     TEXT NOT NULL DEFAULT '',
        suggestion      TEXT NOT NULL DEFAULT '',
        confidence      REAL NOT NULL DEFAULT 0,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # 三轴迁移：旧库补列 + risk_type 转 nullable（停写不删列）
    "ALTER TABLE risk_items ADD COLUMN IF NOT EXISTS opinion_type TEXT NOT NULL DEFAULT '提醒'",
    "ALTER TABLE risk_items ADD COLUMN IF NOT EXISTS review_dimension TEXT NOT NULL DEFAULT '内容合法性'",
    "ALTER TABLE risk_items ALTER COLUMN risk_type DROP NOT NULL",
    "ALTER TABLE risk_items DROP CONSTRAINT IF EXISTS risk_items_risk_level_check",
    """
    UPDATE risk_items
    SET risk_level = LOWER(risk_level)
    WHERE LOWER(risk_level) IN ('low','medium','high','critical')
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conname = 'risk_items_risk_level_check'
              AND conrelid = 'risk_items'::regclass
        ) THEN
            ALTER TABLE risk_items
            ADD CONSTRAINT risk_items_risk_level_check
            CHECK (risk_level IN ('none','low','medium','high','critical'));
        END IF;
    END $$;
    """,
    "CREATE INDEX IF NOT EXISTS idx_risks_contract_id ON risk_items(contract_id)",
    "CREATE INDEX IF NOT EXISTS idx_risks_clause_id_ref ON risk_items(clause_id_ref)",
    """
    CREATE TABLE IF NOT EXISTS risk_citations (
        id              BIGSERIAL PRIMARY KEY,
        risk_id         BIGINT NOT NULL REFERENCES risk_items(id) ON DELETE CASCADE,
        law_name        TEXT NOT NULL DEFAULT '',
        article_no      TEXT NOT NULL DEFAULT '',
        citation_text   TEXT NOT NULL DEFAULT '',
        chunk_id        TEXT NOT NULL DEFAULT '',
        excerpt         TEXT NOT NULL DEFAULT '',
        verified        BOOLEAN NOT NULL DEFAULT false
    )
    """,
    # 既有库补列（新范式：是否在本地法库核实到该条文）
    "ALTER TABLE risk_citations ADD COLUMN IF NOT EXISTS verified BOOLEAN NOT NULL DEFAULT false",
    "CREATE INDEX IF NOT EXISTS idx_citations_risk_id ON risk_citations(risk_id)",
    # 引用唯一性改按 (risk_id, law_name, article_no)：未核实引用 chunk_id 为空不再互相冲突
    "DROP INDEX IF EXISTS uq_risk_citations_risk_chunk",
    """
    DELETE FROM risk_citations c
    USING risk_citations keep
    WHERE c.risk_id = keep.risk_id
      AND c.law_name = keep.law_name
      AND c.article_no = keep.article_no
      AND c.id > keep.id
    """,
    """
    CREATE TABLE IF NOT EXISTS review_opinions (
        id               BIGSERIAL PRIMARY KEY,
        contract_id      BIGINT NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
        clause_id_ref    BIGINT NOT NULL REFERENCES contract_clauses(id) ON DELETE CASCADE,
        opinion_type     TEXT NOT NULL,
        review_dimension TEXT NOT NULL,
        finding          TEXT NOT NULL DEFAULT '',
        recommendation   TEXT NOT NULL DEFAULT '',
        confidence       REAL NOT NULL DEFAULT 0,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_review_opinions_contract_id ON review_opinions(contract_id)",
    "CREATE INDEX IF NOT EXISTS idx_review_opinions_clause_id_ref ON review_opinions(clause_id_ref)",
    """
    CREATE TABLE IF NOT EXISTS review_opinion_citations (
        id              BIGSERIAL PRIMARY KEY,
        opinion_id      BIGINT NOT NULL REFERENCES review_opinions(id) ON DELETE CASCADE,
        law_name        TEXT NOT NULL DEFAULT '',
        article_no      TEXT NOT NULL DEFAULT '',
        citation_text   TEXT NOT NULL DEFAULT '',
        chunk_id        TEXT NOT NULL DEFAULT '',
        excerpt         TEXT NOT NULL DEFAULT '',
        verified        BOOLEAN NOT NULL DEFAULT false
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_review_opinion_citations_opinion_id ON review_opinion_citations(opinion_id)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_review_opinion_citations_law_article
    ON review_opinion_citations(opinion_id, law_name, article_no)
    """,
    """
    CREATE TABLE IF NOT EXISTS clause_risk_assessments (
        id              BIGSERIAL PRIMARY KEY,
        contract_id     BIGINT NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
        clause_id_ref   BIGINT NOT NULL REFERENCES contract_clauses(id) ON DELETE CASCADE,
        risk_level      TEXT NOT NULL CHECK (risk_level IN ('none','low','medium','high','critical')),
        rationale       TEXT NOT NULL DEFAULT '',
        affected_party  TEXT NOT NULL DEFAULT '未知',
        confidence      REAL NOT NULL DEFAULT 0,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(contract_id, clause_id_ref)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_clause_risk_assessments_contract_id ON clause_risk_assessments(contract_id)",
    """
    CREATE TABLE IF NOT EXISTS contract_consistency_facts (
        id               BIGSERIAL PRIMARY KEY,
        contract_id      BIGINT NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
        clause_id_ref    BIGINT NOT NULL REFERENCES contract_clauses(id) ON DELETE CASCADE,
        category         TEXT NOT NULL,
        fact_key         TEXT NOT NULL DEFAULT '',
        party            TEXT NOT NULL DEFAULT '未知',
        value_text       TEXT NOT NULL DEFAULT '',
        normalized_value TEXT NOT NULL DEFAULT '',
        span_text        TEXT NOT NULL DEFAULT '',
        related_text     TEXT NOT NULL DEFAULT '',
        confidence       REAL NOT NULL DEFAULT 0,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_consistency_facts_contract_id ON contract_consistency_facts(contract_id)",
    "CREATE INDEX IF NOT EXISTS idx_consistency_facts_clause_id_ref ON contract_consistency_facts(clause_id_ref)",
    """
    CREATE TABLE IF NOT EXISTS contract_consistency_opinions (
        id                      BIGSERIAL PRIMARY KEY,
        contract_id             BIGINT NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
        opinion_type            TEXT NOT NULL,
        review_dimension        TEXT NOT NULL,
        finding                 TEXT NOT NULL DEFAULT '',
        recommendation          TEXT NOT NULL DEFAULT '',
        related_clause_ids_json TEXT NOT NULL DEFAULT '[]',
        evidence_facts_json     TEXT NOT NULL DEFAULT '[]',
        confidence              REAL NOT NULL DEFAULT 0,
        created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_consistency_opinions_contract_id ON contract_consistency_opinions(contract_id)",
    """
    CREATE TABLE IF NOT EXISTS contract_consistency_risk_assessments (
        id             BIGSERIAL PRIMARY KEY,
        contract_id    BIGINT NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
        risk_level     TEXT NOT NULL CHECK (risk_level IN ('none','low','medium','high','critical')),
        rationale      TEXT NOT NULL DEFAULT '',
        affected_party TEXT NOT NULL DEFAULT '未知',
        confidence     REAL NOT NULL DEFAULT 0,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(contract_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_consistency_risk_assessments_contract_id ON contract_consistency_risk_assessments(contract_id)",
    """
    DELETE FROM contract_clauses c
    USING contract_clauses keep
    WHERE c.contract_id = keep.contract_id
      AND c.clause_id = keep.clause_id
      AND c.id > keep.id
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_contract_clauses_contract_clause
    ON contract_clauses(contract_id, clause_id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_risk_citations_risk_article
    ON risk_citations(risk_id, law_name, article_no)
    """,
)


def init_schema() -> None:
    """创建所有表（幂等），应用启动时调用一次。"""
    with get_conn() as conn, conn.cursor() as cur:
        for stmt in _SCHEMA_STATEMENTS:
            cur.execute(stmt)


def close_pool() -> None:
    """关闭连接池（应用退出时调用）。"""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


__all__ = ["get_conn", "init_schema", "close_pool"]
