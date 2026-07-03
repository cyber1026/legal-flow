"""法律文档入库 API。

Pipeline：上传 .docx → 保存到 data/raw/ → Docling 解析 → JSONL（data/parsed_chunks/）
         → BGE-M3 向量化 → Milvus law_chunks

Job status 流转：pending → parsing → embedding → done / failed
"""

from __future__ import annotations

import logging
import secrets
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import BaseModel

from app.auth.deps import get_current_user
from app.auth.models import UserPublic
from app.core.config import settings
from app.ingest.job_store import LawIngestJobRecord, LawIngestJobStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["law-ingest"])


# ---------------------------------------------------------------------------
# Job model（法律入库特有，含三阶段进度字段）
# ---------------------------------------------------------------------------

class LawIngestJob(BaseModel):
    job_id: str
    filename: str
    law_name: str | None = None          # 解析后得到的法律名称
    status: str                           # pending | parsing | embedding | done | failed
    parsed_chunks: int | None = None      # 解析阶段产生的 chunk 数
    embedded_chunks: int | None = None    # 已写入 Milvus 的 chunk 数
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


def _job_to_api(job: LawIngestJobRecord) -> LawIngestJob:
    return LawIngestJob(
        job_id=job.job_id,
        filename=job.filename,
        law_name=job.law_name,
        status=job.status,
        parsed_chunks=job.parsed_chunks,
        embedded_chunks=job.embedded_chunks,
        error=job.error,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


# ---------------------------------------------------------------------------
# 后台三阶段 Pipeline
# ---------------------------------------------------------------------------

def _run_law_pipeline(job_id: str, user_id: int, docx_path: Path) -> None:
    """后台任务：Docling 解析 → JSONL → Milvus 写入。"""
    from app.ingest.law_ingest import LawIngestPipeline, build_law_vector_store
    from scripts.parse_laws import process_file

    parsed_dir = Path(settings.law_parsed_dir)
    parsed_dir.mkdir(parents=True, exist_ok=True)

    # ── 阶段1：Docling 解析 → JSONL ────────────────────────────────────────
    LawIngestJobStore.update(job_id, user_id=user_id, status="parsing")
    try:
        n_parsed = process_file(docx_path, parsed_dir)
        if n_parsed < 0:
            LawIngestJobStore.update(
                job_id,
                user_id=user_id,
                status="failed",
                error="Docling 解析失败",
                finish=True,
            )
            return
        if n_parsed == 0:
            LawIngestJobStore.update(
                job_id,
                user_id=user_id,
                status="failed",
                error="未从文档中提取到任何法条",
                finish=True,
            )
            return
    except Exception as exc:
        logger.exception("解析阶段失败 job=%s", job_id)
        LawIngestJobStore.update(
            job_id,
            user_id=user_id,
            status="failed",
            error=f"解析错误: {exc}",
            finish=True,
        )
        return

    # 获取法律名称（文件名去掉日期后缀）
    from scripts.parse_laws import parse_filename
    law_name, _, _ = parse_filename(docx_path)
    LawIngestJobStore.update(
        job_id,
        user_id=user_id,
        parsed_chunks=n_parsed,
        law_name=law_name,
    )

    # ── 阶段2：JSONL → Milvus ──────────────────────────────────────────────
    LawIngestJobStore.update(job_id, user_id=user_id, status="embedding")
    try:
        jsonl_path = parsed_dir / f"{law_name}.jsonl"
        vs = build_law_vector_store(drop_old=False)
        pipeline = LawIngestPipeline(vector_store=vs)
        n_embedded = pipeline.ingest_jsonl(jsonl_path)
    except Exception as exc:
        logger.exception("Embedding 阶段失败 job=%s", job_id)
        LawIngestJobStore.update(
            job_id,
            user_id=user_id,
            status="failed",
            error=f"向量化错误: {exc}",
            finish=True,
        )
        return

    LawIngestJobStore.update(
        job_id,
        user_id=user_id,
        status="done",
        embedded_chunks=n_embedded,
        finish=True,
    )
    logger.info(
        "法律入库完成 job=%s law=%s parsed=%d embedded=%d",
        job_id,
        law_name,
        n_parsed,
        n_embedded,
    )


# ---------------------------------------------------------------------------
# API 路由
# ---------------------------------------------------------------------------

@router.post(
    "/law-ingest",
    response_model=LawIngestJob,
    status_code=status.HTTP_202_ACCEPTED,
    summary="上传法律 docx 文件并一键解析入库",
)
async def law_ingest_file(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    current: UserPublic = Depends(get_current_user),
) -> LawIngestJob:
    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="缺少文件名")

    suffix = Path(file.filename).suffix.lower()
    if suffix != ".docx":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="仅支持 .docx 格式的法律文件",
        )

    raw_dir = Path(settings.law_raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    target = raw_dir / Path(file.filename).name

    try:
        with target.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
    finally:
        await file.close()

    job_id = secrets.token_urlsafe(8)
    job = LawIngestJobStore.create(
        job_id=job_id,
        user_id=current.id,
        filename=target.name,
        status="pending",
    )

    background_tasks.add_task(_run_law_pipeline, job_id, current.id, target)
    return _job_to_api(job)


@router.get(
    "/law-ingest/jobs/{job_id}",
    response_model=LawIngestJob,
    summary="查询法律入库任务状态",
)
async def get_law_job(
    job_id: str,
    current: UserPublic = Depends(get_current_user),
) -> LawIngestJob:
    job = LawIngestJobStore.get_owned(job_id, current.id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    return _job_to_api(job)


@router.get(
    "/law-ingest/jobs",
    response_model=list[LawIngestJob],
    summary="列出当前用户的法律入库任务",
)
async def list_law_jobs(
    current: UserPublic = Depends(get_current_user),
) -> list[LawIngestJob]:
    return [_job_to_api(j) for j in LawIngestJobStore.list_for_user(current.id)]


# ---------------------------------------------------------------------------
# 已入库法律列表 & 删除
# ---------------------------------------------------------------------------

class LawItem(BaseModel):
    """已入库的一部法律的摘要信息。"""
    law_name: str
    doc_id: str
    chunk_count: int
    effective_date: str
    version: str
    law_status: str          # "effective" 等（避免与 HTTP status 混淆）


class LawChunkDetail(BaseModel):
    """一条法律 chunk 的详细信息（用于前端法条浏览器）。"""
    chunk_id: str
    chunk_index: int
    article_no: str
    article_text: str        # 原始条文正文
    embedding_text: str      # 向量化文本（含层级上下文）
    part: str                # 编（空字符串表示无）
    chapter: str             # 章（空字符串表示无）
    section: str             # 节（空字符串表示无）
    citation_text: str
    char_count: int


class LawFileChunks(BaseModel):
    """一部法律所有条文 chunk 的聚合响应。"""
    law_name: str
    total_chunks: int
    effective_date: str
    version: str
    chunks: list[LawChunkDetail]


_CN_DIGIT: dict[str, int] = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNIT: dict[str, int] = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def _cn_num_to_int(s: str) -> int:
    """将中文数字字符串（如"一百零三"）转换为整数 103。"""
    result = 0
    temp = 0
    for ch in s:
        if ch in _CN_DIGIT:
            temp = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            result += (temp or 1) * unit
            temp = 0
    return result + temp


def _extract_ordinal(text: str) -> int:
    """从"第X编/章/节/条"形式的字符串中提取序号整数，用于排序。"""
    import re
    m = re.search(r"第([零〇一二三四五六七八九十百千万]+)", text or "")
    if not m:
        return 0
    return _cn_num_to_int(m.group(1))


def _collect_laws() -> list[LawItem]:
    """从 law_chunks collection 扫描出所有已入库法律的摘要。"""
    from app.ingest.law_ingest import get_law_vector_store

    results: dict[str, dict] = defaultdict(lambda: {
        "doc_id": "", "chunk_count": 0,
        "effective_date": "", "version": "", "law_status": "effective",
    })
    try:
        vs = get_law_vector_store()
        col = vs.col
        if col is None:
            return []
        iterator = col.query_iterator(
            expr="",
            output_fields=["law_name", "doc_id", "effective_date", "version", "status"],
            batch_size=1000,
        )
        try:
            while True:
                batch = iterator.next()
                if not batch:
                    break
                for row in batch:
                    name = row.get("law_name") or ""
                    if not name:
                        continue
                    entry = results[name]
                    entry["chunk_count"] += 1
                    if not entry["doc_id"]:
                        entry["doc_id"] = row.get("doc_id") or name
                    if not entry["effective_date"]:
                        entry["effective_date"] = row.get("effective_date") or ""
                    if not entry["version"]:
                        entry["version"] = row.get("version") or ""
                    if not entry["law_status"]:
                        entry["law_status"] = row.get("status") or "effective"
        finally:
            iterator.close()
    except Exception:
        logger.exception("扫描 law_chunks collection 失败")
    return [
        LawItem(law_name=name, **info)
        for name, info in sorted(results.items())
    ]


@router.get(
    "/law-ingest/laws/{law_name:path}/chunks",
    response_model=LawFileChunks,
    summary="查看指定法律的所有条文 chunk 详情",
)
async def list_law_chunks(
    law_name: str,
    current: UserPublic = Depends(get_current_user),  # noqa: ARG001
) -> LawFileChunks:
    from app.ingest.law_ingest import get_law_vector_store

    _LAW_CHUNK_FIELDS = [
        "chunk_id", "article_no", "article_text", "text",
        "embedding_text", "part", "chapter", "section",
        "citation_text", "effective_date", "version",
    ]

    try:
        vs = get_law_vector_store()
        col = vs.col
        if col is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="法律知识库为空")
        safe = law_name.replace('"', '\\"')
        iterator = col.query_iterator(
            expr=f'law_name == "{safe}"',
            output_fields=_LAW_CHUNK_FIELDS,
            batch_size=500,
        )
        rows: list[dict] = []
        try:
            while True:
                batch = iterator.next()
                if not batch:
                    break
                rows.extend(batch)
        finally:
            iterator.close()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to fetch law chunks for %s", law_name)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"查询失败: {exc}",
        ) from exc

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到《{law_name}》的法条，请确认该法律已入库",
        )

    # 按 编 → 章 → 节 → 条 排序（中文序数转整数以正确排序）
    rows.sort(key=lambda r: (
        _extract_ordinal(r.get("part") or ""),
        _extract_ordinal(r.get("chapter") or ""),
        _extract_ordinal(r.get("section") or ""),
        _extract_ordinal(r.get("article_no") or ""),
    ))

    effective_date = rows[0].get("effective_date") or ""
    version = rows[0].get("version") or ""

    details: list[LawChunkDetail] = []
    for i, row in enumerate(rows):
        article_text = row.get("article_text") or row.get("text") or ""
        embed_text   = row.get("embedding_text") or row.get("text") or ""
        details.append(LawChunkDetail(
            chunk_id      = str(row.get("chunk_id") or f"chunk-{i:05d}"),
            chunk_index   = i,
            article_no    = str(row.get("article_no") or ""),
            article_text  = article_text,
            embedding_text= embed_text,
            part          = str(row.get("part") or ""),
            chapter       = str(row.get("chapter") or ""),
            section       = str(row.get("section") or ""),
            citation_text = str(row.get("citation_text") or ""),
            char_count    = len(article_text),
        ))

    return LawFileChunks(
        law_name      = law_name,
        total_chunks  = len(details),
        effective_date= effective_date,
        version       = version,
        chunks        = details,
    )


@router.get(
    "/law-ingest/laws",
    response_model=list[LawItem],
    summary="列出所有已入库法律",
)
async def list_laws(
    current: UserPublic = Depends(get_current_user),  # noqa: ARG001
) -> list[LawItem]:
    return _collect_laws()


@router.delete(
    "/law-ingest/laws/{law_name:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="从 law_chunks collection 删除指定法律的所有 chunk",
)
async def delete_law(
    law_name: str,
    current: UserPublic = Depends(get_current_user),  # noqa: ARG001
) -> None:
    from app.ingest.law_ingest import get_law_vector_store

    try:
        vs = get_law_vector_store()
        col = vs.col
        if col is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="法律知识库为空")
        safe = law_name.replace('"', '\\"')
        col.delete(expr=f'law_name == "{safe}"')
        col.flush()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("删除法律 %s 失败", law_name)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"删除失败: {exc}",
        ) from exc

    # 同步删除本地 JSONL 缓存（可选，失败不阻塞）
    jsonl = Path(settings.law_parsed_dir) / f"{law_name}.jsonl"
    if jsonl.exists():
        try:
            jsonl.unlink()
        except OSError:
            logger.warning("无法删除 JSONL 缓存: %s", jsonl)


__all__ = ["router", "LawIngestJob", "LawItem"]
