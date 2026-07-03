from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from app.ingest import job_store
from app.ingest.job_store import LawIngestJobStore


class _FakeDb:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self._tick = 0

    def now(self) -> datetime:
        self._tick += 1
        return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=self._tick)

    @contextmanager
    def get_conn(self):
        yield _FakeConn(self)


class _FakeConn:
    def __init__(self, db: _FakeDb) -> None:
        self.db = db

    @contextmanager
    def cursor(self):
        yield _FakeCursor(self.db)


class _FakeCursor:
    def __init__(self, db: _FakeDb) -> None:
        self.db = db
        self._result: dict[str, Any] | None = None
        self._results: list[dict[str, Any]] = []

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        if sql.startswith("INSERT INTO law_ingest_jobs"):
            job_id, user_id, filename, status = params
            row = {
                "job_id": job_id,
                "user_id": user_id,
                "filename": filename,
                "law_name": None,
                "status": status,
                "parsed_chunks": None,
                "embedded_chunks": None,
                "error": None,
                "started_at": self.db.now(),
                "finished_at": None,
            }
            self.db.jobs[job_id] = row
            self._result = row.copy()
            return

        if sql.startswith("UPDATE law_ingest_jobs"):
            values = list(params)
            idx = 0
            changes: dict[str, Any] = {}
            for field in ("status", "law_name", "parsed_chunks", "embedded_chunks", "error"):
                if f"{field} = %s" in sql:
                    changes[field] = values[idx]
                    idx += 1
            finish = "finished_at = NOW()" in sql
            job_id = values[idx]
            idx += 1
            user_id = values[idx] if "AND user_id = %s" in sql else None
            row = self.db.jobs.get(job_id)
            if row is None or (user_id is not None and row["user_id"] != user_id):
                self._result = None
                return
            row.update(changes)
            if finish:
                row["finished_at"] = self.db.now()
            self._result = row.copy()
            return

        if "WHERE job_id = %s AND user_id = %s" in sql:
            job_id, user_id = params
            row = self.db.jobs.get(job_id)
            self._result = row.copy() if row and row["user_id"] == user_id else None
            return

        if "WHERE user_id = %s ORDER BY started_at DESC LIMIT %s" in sql:
            user_id, limit = params
            rows = [
                row.copy()
                for row in self.db.jobs.values()
                if row["user_id"] == user_id
            ]
            rows.sort(key=lambda row: row["started_at"], reverse=True)
            self._results = rows[:limit]
            return

        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._results


def test_law_ingest_job_store_persists_and_isolates_by_user(monkeypatch):
    fake = _FakeDb()
    monkeypatch.setattr(job_store, "get_conn", fake.get_conn)

    first = LawIngestJobStore.create(
        job_id="job-a",
        user_id=1,
        filename="a.docx",
    )
    second = LawIngestJobStore.create(
        job_id="job-b",
        user_id=1,
        filename="b.docx",
    )
    LawIngestJobStore.create(
        job_id="job-c",
        user_id=2,
        filename="c.docx",
    )

    updated = LawIngestJobStore.update(
        first.job_id,
        user_id=1,
        status="done",
        law_name="中华人民共和国民法典",
        parsed_chunks=12,
        embedded_chunks=12,
        finish=True,
    )

    assert updated is not None
    assert updated.status == "done"
    assert updated.law_name == "中华人民共和国民法典"
    assert updated.finished_at is not None
    assert LawIngestJobStore.get_owned(first.job_id, user_id=2) is None

    own_jobs = LawIngestJobStore.list_for_user(1)
    assert [job.job_id for job in own_jobs] == [second.job_id, first.job_id]
    assert all(job.user_id == 1 for job in own_jobs)
