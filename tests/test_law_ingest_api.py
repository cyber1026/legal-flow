from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from tempfile import SpooledTemporaryFile
from typing import Any

import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile

from app.api.routes import law_ingest
from app.auth.models import UserPublic
from app.ingest.job_store import LawIngestJobRecord


class _FakeJobStore:
    jobs: dict[str, LawIngestJobRecord] = {}
    tick = 0

    @classmethod
    def _now(cls) -> datetime:
        cls.tick += 1
        return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=cls.tick)

    @classmethod
    def create(
        cls,
        *,
        job_id: str,
        user_id: int,
        filename: str,
        status: str = "pending",
    ) -> LawIngestJobRecord:
        job = LawIngestJobRecord(
            job_id=job_id,
            user_id=user_id,
            filename=filename,
            law_name=None,
            status=status,
            parsed_chunks=None,
            embedded_chunks=None,
            error=None,
            started_at=cls._now(),
            finished_at=None,
        )
        cls.jobs[job_id] = job
        return job

    @classmethod
    def get_owned(cls, job_id: str, user_id: int) -> LawIngestJobRecord | None:
        job = cls.jobs.get(job_id)
        return job if job and job.user_id == user_id else None

    @classmethod
    def list_for_user(cls, user_id: int, limit: int = 100) -> list[LawIngestJobRecord]:
        jobs = [job for job in cls.jobs.values() if job.user_id == user_id]
        jobs.sort(key=lambda job: job.started_at, reverse=True)
        return jobs[:limit]

    @classmethod
    def update(cls, *_args: Any, **_kwargs: Any) -> LawIngestJobRecord | None:
        return None


def _user(user_id: int) -> UserPublic:
    return UserPublic(
        id=user_id,
        email=f"user{user_id}@example.com",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _docx(name: str) -> UploadFile:
    file = SpooledTemporaryFile(max_size=1024)
    file.write(b"fake-docx")
    file.seek(0)
    return UploadFile(file, filename=name)


def test_law_ingest_jobs_are_persisted_and_user_scoped(monkeypatch, tmp_path):
    _FakeJobStore.jobs = {}
    _FakeJobStore.tick = 0

    monkeypatch.setattr(law_ingest, "LawIngestJobStore", _FakeJobStore)
    monkeypatch.setattr(law_ingest.settings, "law_raw_dir", str(tmp_path))

    created = asyncio.run(
        law_ingest.law_ingest_file(
            background_tasks=BackgroundTasks(),
            file=_docx("民法典.docx"),
            current=_user(1),
        )
    )

    owned = asyncio.run(law_ingest.get_law_job(created.job_id, current=_user(1)))
    assert owned.filename == "民法典.docx"

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(law_ingest.get_law_job(created.job_id, current=_user(2)))
    assert exc_info.value.status_code == 404

    asyncio.run(
        law_ingest.law_ingest_file(
            background_tasks=BackgroundTasks(),
            file=_docx("劳动法.docx"),
            current=_user(2),
        )
    )

    listed = asyncio.run(law_ingest.list_law_jobs(current=_user(2)))
    assert [job.filename for job in listed] == ["劳动法.docx"]
