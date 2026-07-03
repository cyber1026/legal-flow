"""可观测性中枢：统一日志配置 + correlation_id 关联 + LangSmith tracing 接入。

本模块把「应用日志」与「LangSmith trace」用同一个 ``correlation_id`` 绑定起来，二者互补：

- **LangSmith** 负责「大脑内部」：LLM/Agent/Tool 的执行图、prompt/输出、token、延迟、工具入出参。
- **应用日志** 负责「大脑周边」：生命周期、HTTP 请求、DB/OCR/解析、异常堆栈、非 LLM 步骤耗时。

协作机制：每个顶层操作（一次 chat / 一次合同审查）生成一个 uuid4 作为 ``correlation_id``，
写进每一行日志（经 :class:`CorrelationIdFilter`），同时作为 LangChain **根 run_id** 传给 agent/graph
（见 :func:`build_run_config` 的 ``root=True``）。于是 trace 的 run_id == 日志里的 corr_id，可双向跳转。

为避免重复：应用日志只记「事件 + ID + 指标」，不打印 prompt / 模型正文（那是 LangSmith 的职责）。
"""

from __future__ import annotations

import logging
import logging.config
import os
import time
import uuid
from contextvars import ContextVar
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import settings

logger = logging.getLogger(__name__)

# 请求级日志专用 logger（每个 HTTP 请求一行汇总）。
_request_logger = logging.getLogger("app.request")


# ---------------------------------------------------------------------------
# correlation_id：贯穿日志与 LangSmith trace 的关联 ID
# ---------------------------------------------------------------------------

# 默认空串；HTTP 中间件 / 审查任务入口会在各自上下文里 set 一个 uuid4。
_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def new_correlation_id() -> str:
    """生成一个新的 correlation_id（uuid4 字符串）并写入当前上下文，返回之。"""
    cid = str(uuid.uuid4())
    _correlation_id.set(cid)
    return cid


def get_correlation_id() -> str:
    """读取当前上下文的 correlation_id；无则返回空串。"""
    return _correlation_id.get()


class CorrelationIdFilter(logging.Filter):
    """给每条日志记录注入 ``correlation_id`` 字段，供格式化串引用。

    无上下文（如启动期日志）时填 ``-``，保证格式串永远有值不会 KeyError。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"
        return True


# ---------------------------------------------------------------------------
# 日志配置（标准库 logging.config.dictConfig；富文本格式 + 滚动文件持久化）
# ---------------------------------------------------------------------------

# 富文本格式：时间 | 级别 | 模块名 | corr_id | 正文。控制台与文件保持一致，便于肉眼/grep 排查。
_LOG_FORMAT = "%(asctime)s %(levelname)-5s [%(name)s] [corr=%(correlation_id)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging() -> None:
    """配置 root logger：控制台 + 滚动文件双 handler，并接管 uvicorn 日志。

    幂等：可在 reload 时重复调用。读取 ``settings`` 里的 log_level / log_dir 等配置。
    """
    handlers: dict[str, dict[str, Any]] = {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "rich",
            "filters": ["correlation_id"],
        }
    }
    handler_names = ["console"]

    if settings.log_to_file:
        os.makedirs(settings.log_dir, exist_ok=True)
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "rich",
            "filters": ["correlation_id"],
            "filename": os.path.join(settings.log_dir, "legal_flow.log"),
            "maxBytes": settings.log_file_max_mb * 1024 * 1024,
            "backupCount": settings.log_file_backup_count,
            "encoding": "utf-8",
        }
        handlers["contract_audit_file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "audit",
            "filters": ["correlation_id"],
            "filename": os.path.join(settings.log_dir, "contract_review_audit.jsonl"),
            "maxBytes": settings.log_file_max_mb * 1024 * 1024,
            "backupCount": settings.log_file_backup_count,
            "encoding": "utf-8",
        }
        handler_names.append("file")

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "correlation_id": {"()": "app.core.observability.CorrelationIdFilter"},
            },
            "formatters": {
                "rich": {"format": _LOG_FORMAT, "datefmt": _DATE_FORMAT},
                "audit": {"format": "%(message)s"},
            },
            "handlers": handlers,
            "root": {"level": settings.log_level.upper(), "handlers": handler_names},
            "loggers": {
                # uvicorn 启动/错误日志接入同一套 handler；propagate=False 避免与 root 重复输出。
                "uvicorn": {"level": "INFO", "handlers": handler_names, "propagate": False},
                "uvicorn.error": {"level": "INFO", "handlers": handler_names, "propagate": False},
                # access 日志由 RequestLoggingMiddleware 替代（带 corr_id/耗时），这里压到 WARNING 防重复。
                "uvicorn.access": {"level": "WARNING", "handlers": handler_names, "propagate": False},
                "app.contracts.audit": {
                    "level": "INFO",
                    "handlers": ["contract_audit_file"] if settings.log_to_file else handler_names,
                    "propagate": False,
                },
            },
        }
    )


# ---------------------------------------------------------------------------
# LangSmith tracing 初始化
# ---------------------------------------------------------------------------

def init_langsmith() -> None:
    """按 settings 把 LangSmith 配置写入环境变量（SDK 只读 os.environ）。

    默认 ``langsmith_tracing=False`` 时彻底关闭，无 key 也不影响应用运行。
    """
    if not settings.langsmith_tracing:
        logger.info("LangSmith tracing 未开启（settings.langsmith_tracing=False）")
        return

    os.environ["LANGSMITH_TRACING"] = "true"
    if settings.langsmith_api_key:
        os.environ.setdefault("LANGSMITH_API_KEY", settings.langsmith_api_key)
    os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)
    os.environ.setdefault("LANGSMITH_ENDPOINT", settings.langsmith_endpoint)

    if not settings.langsmith_api_key and not os.environ.get("LANGSMITH_API_KEY"):
        logger.warning("LangSmith tracing 已开启但缺少 LANGSMITH_API_KEY，trace 将无法上报")
    else:
        logger.info("LangSmith tracing 已开启 project=%s", settings.langsmith_project)


def log_observability_status() -> None:
    """启动时打印一行可观测性状态，便于确认配置是否生效。"""
    logger.info(
        "可观测性就绪 log_level=%s log_file=%s langsmith=%s project=%s",
        settings.log_level.upper(),
        os.path.join(settings.log_dir, "legal_flow.log") if settings.log_to_file else "(off)",
        "on" if settings.langsmith_tracing else "off",
        settings.langsmith_project if settings.langsmith_tracing else "-",
    )


# ---------------------------------------------------------------------------
# LangChain 调用 config 工厂（统一注入 metadata / tags / run_name / 根 run_id）
# ---------------------------------------------------------------------------

def build_run_config(
    *,
    run_name: str,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    root: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """构造传给 LangChain ``ainvoke/astream/astream_events`` 的 config。

    - ``root=True``：把当前 correlation_id 作为根 run_id，使 LangSmith trace 的 run_id == 日志 corr_id。
      **仅最外层调用（chat 的 agent / 审查的 graph）应传 root=True**；嵌套调用（分类/单条款/总览）
      不设 run_id，靠 LangChain 经 contextvar 传播 callbacks 自动挂到父 run 下，否则 run_id 冲突。
    - ``extra``：透传 recursion_limit / max_concurrency / configurable 等其它 config 字段。
    """
    cfg: dict[str, Any] = {"run_name": run_name}
    if tags:
        cfg["tags"] = list(tags)

    md = dict(metadata or {})
    corr = get_correlation_id()
    if corr:
        md.setdefault("correlation_id", corr)
    cfg["metadata"] = md

    if root and corr:
        try:
            cfg["run_id"] = uuid.UUID(corr)  # LangChain 要求 run_id 为 UUID 类型
        except ValueError:
            pass

    cfg.update(extra)
    return cfg


# ---------------------------------------------------------------------------
# HTTP 请求级日志中间件
# ---------------------------------------------------------------------------

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """每个 HTTP 请求记一行汇总日志，并在请求入口生成 correlation_id。

    - 入口 ``new_correlation_id()``：该请求内（含下游 route handler）的所有日志共享同一 corr_id；
      chat 与请求同任务，会自然复用此 corr_id，使 日志 ↔ trace ↔ HTTP 三者同 ID。
    - 出口记录：方法 / 路径 / 状态码 / 耗时；corr_id 由日志格式串自动带出。
    - 响应头回填 ``X-Correlation-ID``，便于前端与排查关联。
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        corr = new_correlation_id()
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            dur_ms = (time.perf_counter() - start) * 1000
            _request_logger.exception(
                "%s %s 请求异常 dur=%.0fms", request.method, request.url.path, dur_ms
            )
            raise
        dur_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Correlation-ID"] = corr
        _request_logger.info(
            "%s %s status=%s dur=%.0fms",
            request.method,
            request.url.path,
            response.status_code,
            dur_ms,
        )
        return response


__all__ = [
    "CorrelationIdFilter",
    "RequestLoggingMiddleware",
    "build_run_config",
    "get_correlation_id",
    "init_langsmith",
    "log_observability_status",
    "new_correlation_id",
    "setup_logging",
]
