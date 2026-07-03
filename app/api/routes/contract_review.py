"""合同智能审查 API 路由。

Pipeline：上传文件 → 保存到 data/contracts/raw/ → supervisor 明确发起审查 → 后台跑 ingest + review →
        clauses/opinions/risk assessments 写 PG，向量写 Milvus contract_chunks

Job 状态流转：pending → parsing → embedding → reviewing → done / failed
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import secrets
import shutil
from pathlib import Path

import jwt
from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, StreamingResponse

from app.api.schemas.contract import (
    ClauseRiskAssessmentDTO,
    ConsistencyOpinionDTO,
    ConsistencyRiskAssessmentDTO,
    ContractClauseDTO,
    ContractPartyStanceRequest,
    ContractReport,
    ContractSummary,
    ReviewCitationDTO,
    ReviewOpinionDTO,
)
from app.api.sse import sse_pack
from app.auth.deps import get_current_user, oauth2_scheme
from app.auth.models import UserPublic
from app.auth.security import decode_access_token
from app.auth.store import UserStore
from app.contracts.milvus_store import delete_by_contract
from app.contracts.parser.dispatcher import detect_doc_type
from app.contracts.review_manager import contract_review_manager
from app.contracts.store import ContractRecord, ContractStore
from app.core.config import settings
from app.sessions.store import SessionStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/contract-review", tags=["contract-review"])

# 接受的扩展名（由 dispatcher.detect_doc_type 校验）
_ACCEPTED_EXTS = {
    ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff",
    ".pdf", ".docx",
}

# SSE 心跳间隔：审查首批条款 LLM 调用可能长时间无事件，需主动吐保活避免中间层断连。
# 取 15s 是常见的「比浏览器/反代默认 idle timeout 都小」的稳妥值。
_SSE_KEEPALIVE_S = 15.0


# ---------------------------------------------------------------------------
# Record → DTO 转换
# ---------------------------------------------------------------------------

def _to_summary(rec: ContractRecord) -> ContractSummary:
    return ContractSummary(
        id=rec.id,
        session_id=rec.session_id,
        job_id=rec.job_id,
        filename=rec.filename,
        title=rec.title,
        doc_type=rec.doc_type,
        status=rec.status,
        parsed_clauses=rec.parsed_clauses,
        risk_count=rec.risk_count,
        opinion_count=rec.risk_count,
        error=rec.error,
        party_stance=rec.party_stance,
        started_at=rec.started_at,
        finished_at=rec.finished_at,
        created_at=rec.created_at,
    )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@router.post(
    "",
    response_model=ContractSummary,
    status_code=status.HTTP_202_ACCEPTED,
    summary="上传合同（image/pdf/docx），返回记录；审查需由会话 supervisor 明确发起",
)
async def upload_contract(
    file: UploadFile,
    session_id: str | None = Form(default=None),
    current: UserPublic = Depends(get_current_user),
) -> ContractSummary:
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in _ACCEPTED_EXTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"不支持的文件类型 {suffix}（仅支持 image/pdf/docx）",
        )

    # 合同从属会话：传了 session_id 则校验归属，否则新建一个会话承载本次审查。
    # （放在文件类型校验之后，避免非法上传留下空会话。）
    if session_id:
        sess = SessionStore.get(session_id, current.id)
        if not sess:
            raise HTTPException(status_code=404, detail="会话不存在")
    else:
        sess = SessionStore.create(current.id, title="新会话")

    # 大小限制：先读 content-length（如果客户端给了）；否则保存时再校验
    max_bytes = settings.contract_max_upload_mb * 1024 * 1024

    # 落盘到 data/contracts/raw/{user_id}/{job_id}-{filename}
    job_id = secrets.token_urlsafe(8)
    raw_dir = Path(settings.contract_raw_dir) / str(current.id)
    raw_dir.mkdir(parents=True, exist_ok=True)
    target = raw_dir / f"{job_id}-{Path(file.filename).name}"

    try:
        written = 0
        with target.open("wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    fh.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"文件超过 {settings.contract_max_upload_mb}MB 上限",
                    )
                fh.write(chunk)
    finally:
        await file.close()

    try:
        doc_type = detect_doc_type(target)
    except ValueError as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    mime = file.content_type or mimetypes.guess_type(file.filename)[0] or ""
    filename = Path(file.filename).name

    record = ContractStore.create(
        user_id=current.id,
        session_id=sess.id,
        job_id=job_id,
        filename=filename,
        mime=mime,
        doc_type=doc_type,
        storage_path=str(target),
    )

    # 会话标题仍是默认值时，改为合同名，便于侧边栏识别。
    if sess.title in ("新会话", ""):
        SessionStore.rename(sess.id, current.id, f"合同审查：《{filename}》")

    # 不在此处跑审查：上传只把合同挂载到会话；用户明确要求审查后，由 supervisor 顶层图发起。
    return _to_summary(record)


@router.get(
    "/jobs/{job_id}",
    response_model=ContractSummary,
    summary="按 job_id 轮询任务状态",
)
async def get_job(
    job_id: str,
    current: UserPublic = Depends(get_current_user),
) -> ContractSummary:
    rec = ContractStore.get_by_job(job_id, current.id)
    if not rec:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _to_summary(rec)


@router.get(
    "/contracts",
    response_model=list[ContractSummary],
    summary="列出当前用户的所有合同",
)
async def list_contracts(
    current: UserPublic = Depends(get_current_user),
) -> list[ContractSummary]:
    records = ContractStore.list_for_user(current.id)
    return [_to_summary(r) for r in records]


@router.get(
    "/contracts/{contract_id}",
    response_model=ContractReport,
    summary="合同详情（含条款 + 审查意见 + 风险评估 + 引用）",
)
async def get_contract(
    contract_id: int,
    current: UserPublic = Depends(get_current_user),
) -> ContractReport:
    rec = ContractStore.get_owned(contract_id, current.id)
    if not rec:
        raise HTTPException(status_code=404, detail="合同不存在")

    clauses = ContractStore.list_clauses(contract_id)
    opinions = ContractStore.list_review_opinions(contract_id)
    assessments = ContractStore.list_clause_risk_assessments(contract_id)
    consistency_opinions = ContractStore.list_consistency_opinions(contract_id)
    consistency_risk = ContractStore.get_consistency_risk_assessment(contract_id)

    return ContractReport(
        contract=_to_summary(rec),
        clauses=[
            ContractClauseDTO(
                id=c.id,
                clause_id=c.clause_id,
                section_path=c.section_path,
                clause_no=c.clause_no,
                title=c.title,
                text=c.text,
                page_no=c.page_no,
                bbox=c.bbox,
                chunk_index=c.chunk_index,
                review_status=c.review_status,
                review_has_risk=c.review_has_risk,
                review_has_opinion=c.review_has_risk,
                reasoning=c.reasoning,
            )
            for c in clauses
        ],
        opinions=[
            ReviewOpinionDTO(
                id=o.id,
                clause_id_ref=o.clause_id_ref,
                opinion_type=o.opinion_type,
                review_dimension=o.review_dimension,
                finding=o.finding,
                recommendation=o.recommendation,
                confidence=o.confidence,
                citations=[
                    ReviewCitationDTO(
                        law_name=c.law_name,
                        article_no=c.article_no,
                        citation_text=c.citation_text,
                        chunk_id=c.chunk_id,
                        excerpt=c.excerpt,
                        verified=c.verified,
                    )
                    for c in o.citations
                ],
                created_at=o.created_at,
            )
            for o in opinions
        ],
        clause_risk_assessments=[
            ClauseRiskAssessmentDTO(
                id=a.id,
                clause_id_ref=a.clause_id_ref,
                risk_level=a.risk_level,
                rationale=a.rationale,
                affected_party=a.affected_party,
                confidence=a.confidence,
                created_at=a.created_at,
            )
            for a in assessments
        ],
        consistency_opinions=[
            ConsistencyOpinionDTO(
                id=o.id,
                opinion_type=o.opinion_type,
                review_dimension=o.review_dimension,
                finding=o.finding,
                recommendation=o.recommendation,
                related_clause_ids=o.related_clause_ids,
                evidence_facts=o.evidence_facts,
                confidence=o.confidence,
                created_at=o.created_at,
            )
            for o in consistency_opinions
        ],
        consistency_risk_assessment=(
            ConsistencyRiskAssessmentDTO(
                id=consistency_risk.id,
                risk_level=consistency_risk.risk_level,
                rationale=consistency_risk.rationale,
                affected_party=consistency_risk.affected_party,
                confidence=consistency_risk.confidence,
                created_at=consistency_risk.created_at,
            )
            if consistency_risk
            else None
        ),
    )


@router.get(
    "/contracts/{contract_id}/clauses/{clause_id}",
    response_model=ContractClauseDTO,
    summary="查询单个条款详情（含 bbox，给前端高亮用）",
)
async def get_clause(
    contract_id: int,
    clause_id: str,
    current: UserPublic = Depends(get_current_user),
) -> ContractClauseDTO:
    owner = ContractStore.get_owned(contract_id, current.id)
    if not owner:
        raise HTTPException(status_code=404, detail="合同不存在")
    c = ContractStore.get_clause(contract_id, clause_id)
    if not c:
        raise HTTPException(status_code=404, detail="条款不存在")
    return ContractClauseDTO(
        id=c.id,
        clause_id=c.clause_id,
        section_path=c.section_path,
        clause_no=c.clause_no,
        title=c.title,
        text=c.text,
        page_no=c.page_no,
        bbox=c.bbox,
        chunk_index=c.chunk_index,
    )


@router.patch(
    "/contracts/{contract_id}/party-stance",
    response_model=ContractSummary,
    summary="设置合同委托人立场",
)
async def update_party_stance(
    contract_id: int,
    payload: ContractPartyStanceRequest,
    current: UserPublic = Depends(get_current_user),
) -> ContractSummary:
    rec = ContractStore.get_owned(contract_id, current.id)
    if not rec:
        raise HTTPException(status_code=404, detail="合同不存在")
    ContractStore.update_party_stance(contract_id, payload.party_stance)
    updated = ContractStore.get_owned(contract_id, current.id)
    if not updated:
        raise HTTPException(status_code=404, detail="合同不存在")
    return _to_summary(updated)


def _user_from_token_value(token: str) -> UserPublic:
    """Authenticate a user from a raw JWT string (header or query)."""
    err = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效的认证令牌",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已过期",
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise err from exc

    user_id_str = payload.get("sub")
    if not user_id_str:
        raise err
    try:
        user_id = int(user_id_str)
    except (TypeError, ValueError) as exc:
        raise err from exc

    user = UserStore.get_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    return UserPublic(id=user.id, email=user.email, created_at=user.created_at)


def _current_user_header_or_query(
    header_token: str | None = Depends(oauth2_scheme),
    query_token: str | None = Query(default=None, alias="token"),
) -> UserPublic:
    """Auth dependency accepting token from either Authorization header or ?token=.

    Used only by the file download endpoint so <iframe>/<img> tags can pass the
    JWT via query string when browser navigation cannot set custom headers.
    """
    token = header_token or query_token
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少认证令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _user_from_token_value(token)


@router.get(
    "/contracts/{contract_id}/file",
    summary="下载合同原始文件（PDF/图片/DOCX），供前端预览",
)
async def get_contract_file(
    contract_id: int,
    current: UserPublic = Depends(_current_user_header_or_query),
) -> FileResponse:
    rec = ContractStore.get_owned(contract_id, current.id)
    if not rec:
        raise HTTPException(status_code=404, detail="合同不存在")

    path = Path(rec.storage_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="原始文件已被删除")

    media_type = rec.mime or mimetypes.guess_type(rec.filename)[0] or "application/octet-stream"
    # 让 Starlette 自动用 RFC 5987 处理中文文件名；显式传 inline 让浏览器在
    # iframe / <img> 中直接预览而不是触发下载
    return FileResponse(
        path=path,
        media_type=media_type,
        filename=rec.filename,
        content_disposition_type="inline",
    )


@router.get(
    "/contracts/{contract_id}/stream",
    summary="订阅合同全量审查流（SSE），实时推送 review agent 推理",
)
async def stream_review(
    contract_id: int,
    current: UserPublic = Depends(_current_user_header_or_query),
) -> StreamingResponse:
    """订阅 supervisor 已发起的合同审查，并以 SSE 实时推送每条款的 review agent 推理。

    本端点不再启动 pending 合同的审查任务；审查只能由会话顶层图的 supervisor 发起。
    客户端断开只会取消本次订阅，不会中断已存在的审查任务。鉴权接受 Authorization 头或
    ``?token=``（EventSource 不能设自定义头）。
    """
    rec = ContractStore.get_owned(contract_id, current.id)
    if not rec:
        raise HTTPException(status_code=404, detail="合同不存在")

    async def event_source():
        yield sse_pack("session", {"contract_id": contract_id})
        if rec.status == "done":
            yield sse_pack("done", {"status": "done", "risk_count": rec.risk_count})
            return
        if not await contract_review_manager.is_active(contract_id):
            yield sse_pack(
                "review_not_started",
                {
                    "contract_id": contract_id,
                    "message": "合同已挂载到会话，请在聊天中明确要求审查后再订阅审查流。",
                },
            )
            return
        if rec.party_stance not in ("甲方", "乙方", "中立"):
            yield sse_pack(
                "stance_required",
                {
                    "contract_id": contract_id,
                    "options": ["甲方", "乙方", "中立"],
                },
            )
            return

        # 心跳保活：审查首批条款的 LLM 调用阶段可能数十秒无事件（DeepSeek reasoning
        # 大输入下首 chunk 延迟显著），浏览器 / 反代会在 30-60s idle 后掐 SSE 连接。
        # 实现：开一个 pump 协程把 manager 事件转到本地 inbox，主循环用 wait_for(inbox.get)
        # 带超时——超时就吐一行 SSE 注释（":..." 行不会派发到 EventSource.onmessage，
        # 但能让中间链路保持活跃）。pump 与主循环解耦，wait_for 取消不会污染 subscribe 生成器。
        inbox: asyncio.Queue[dict | object] = asyncio.Queue()
        _END = object()

        async def _pump():
            try:
                async for ev in contract_review_manager.subscribe(contract_id):
                    await inbox.put(ev)
            except Exception as exc:  # noqa: BLE001
                await inbox.put({"event": "error",
                                 "data": {"message": f"{type(exc).__name__}: {exc}"}})
            finally:
                await inbox.put(_END)

        pump_task = asyncio.create_task(_pump(), name=f"sse-pump-{contract_id}")
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(inbox.get(), _SSE_KEEPALIVE_S)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if ev is _END:
                    break
                assert isinstance(ev, dict)
                yield sse_pack(ev["event"], ev.get("data") or {})
        except Exception as exc:  # pragma: no cover - best-effort surface
            logger.exception("审查流崩溃 contract=%s", contract_id)
            yield sse_pack("error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            pump_task.cancel()
            try:
                await pump_task
            except BaseException:  # noqa: BLE001  cancel/取消传播
                pass

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        event_source(), media_type="text/event-stream", headers=headers
    )


@router.delete(
    "/contracts/{contract_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除合同（级联清理 PG + Milvus）",
)
async def delete_contract(
    contract_id: int,
    current: UserPublic = Depends(get_current_user),
) -> None:
    rec = ContractStore.get_owned(contract_id, current.id)
    if not rec:
        raise HTTPException(status_code=404, detail="合同不存在")

    # 先删 Milvus（失败不阻塞 PG 删除，避免脏 PG 数据残留）
    try:
        delete_by_contract(contract_id)
    except Exception:
        logger.exception("删除合同向量失败 contract=%s", contract_id)

    # PG 级联删除会清掉 clauses / opinions / assessments / citations
    ContractStore.delete(contract_id, current.id)

    # 顺手把磁盘文件清理掉（best effort）
    try:
        Path(rec.storage_path).unlink(missing_ok=True)
    except OSError:
        logger.warning("无法删除原始合同文件 %s", rec.storage_path)


__all__ = ["router"]
