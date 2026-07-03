"""法律入库任务的 PostgreSQL 持久化层。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.datetime_utils import to_utc_datetime
from app.db import get_conn


@dataclass(slots=True)
class LawIngestJobRecord:
    job_id: str
    user_id: int
    filename: str
    law_name: Optional[str]
    status: str
    parsed_chunks: Optional[int]
    embedded_chunks: Optional[int]
    error: Optional[str]
    started_at: datetime
    finished_at: Optional[datetime]


_JOB_COLS = (
    "job_id, user_id, filename, law_name, status, parsed_chunks, "
    "embedded_chunks, error, started_at, finished_at"
)


def _row_to_job(row) -> LawIngestJobRecord:
    return LawIngestJobRecord(
        job_id=row["job_id"],
        user_id=row["user_id"],
        filename=row["filename"],
        law_name=row["law_name"],
        status=row["status"],
        parsed_chunks=row["parsed_chunks"],
        embedded_chunks=row["embedded_chunks"],
        error=row["error"],
        started_at=to_utc_datetime(row["started_at"]),
        finished_at=to_utc_datetime(row["finished_at"]) if row["finished_at"] else None,
    )


class LawIngestJobStore:
    """Law ingest job 的 CRUD 静态类。"""

    @staticmethod
    def create(
        *,
        job_id: str,
        user_id: int,
        filename: str,
        status: str = "pending",
    ) -> LawIngestJobRecord:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO law_ingest_jobs(job_id, user_id, filename, status) "
                "VALUES(%s, %s, %s, %s) "
                f"RETURNING {_JOB_COLS}",
                (job_id, user_id, filename, status),
            )
            row = cur.fetchone()
        return _row_to_job(row)

    @staticmethod
    def update(
        job_id: str,
        *,
        user_id: int | None = None,
        status: str | None = None,
        law_name: str | None = None,
        parsed_chunks: int | None = None,
        embedded_chunks: int | None = None,
        error: str | None = None,
        finish: bool = False,
    ) -> LawIngestJobRecord | None:
        sets: list[str] = []
        params: list[object] = []
        if status is not None:
            sets.append("status = %s")
            params.append(status)
        if law_name is not None:
            sets.append("law_name = %s")
            params.append(law_name)
        if parsed_chunks is not None:
            sets.append("parsed_chunks = %s")
            params.append(parsed_chunks)
        if embedded_chunks is not None:
            sets.append("embedded_chunks = %s")
            params.append(embedded_chunks)
        if error is not None:
            sets.append("error = %s")
            params.append(error)
        if finish:
            sets.append("finished_at = NOW()")
        if not sets:
            return LawIngestJobStore.get_owned(job_id, user_id) if user_id else None

        params.append(job_id)
        where = "job_id = %s"
        if user_id is not None:
            where += " AND user_id = %s"
            params.append(user_id)

        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE law_ingest_jobs SET {', '.join(sets)} "
                f"WHERE {where} RETURNING {_JOB_COLS}",
                tuple(params),
            )
            row = cur.fetchone()
        return _row_to_job(row) if row else None

    @staticmethod
    def get_owned(job_id: str, user_id: int) -> LawIngestJobRecord | None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_JOB_COLS} FROM law_ingest_jobs "
                "WHERE job_id = %s AND user_id = %s",
                (job_id, user_id),
            )
            row = cur.fetchone()
        return _row_to_job(row) if row else None

    @staticmethod
    def list_for_user(user_id: int, limit: int = 100) -> list[LawIngestJobRecord]:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_JOB_COLS} FROM law_ingest_jobs "
                "WHERE user_id = %s ORDER BY started_at DESC LIMIT %s",
                (user_id, limit),
            )
            rows = cur.fetchall()
        return [_row_to_job(r) for r in rows]


__all__ = ["LawIngestJobRecord", "LawIngestJobStore"]
