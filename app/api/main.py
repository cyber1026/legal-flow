"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import (
    auth as auth_routes,
    chat as chat_routes,
    contract_review as contract_review_routes,
    health as health_routes,
    law_ingest as law_ingest_routes,
    sessions as sessions_routes,
)
from app.core.checkpointer import close_checkpointer, init_checkpointer
from app.core.config import settings
from app.core.observability import RequestLoggingMiddleware, log_observability_status
from app.db import close_pool, init_schema

logger = logging.getLogger(__name__)


def _mask_dsn(dsn: str) -> str:
    """日志中隐藏 DSN 里的密码部分。"""
    if "@" not in dsn or "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        creds = f"{user}:***"
    return f"{scheme}://{creds}@{host}"


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    log_observability_status()
    init_schema()
    await init_checkpointer()
    logger.info("Legal Flow API starting (db=%s)", _mask_dsn(settings.database_url))
    if settings.uses_default_jwt_secret:
        logger.warning(
            "JWT_SECRET 仍为开发默认值，请勿用于生产；生产请设 environment=production "
            "并配置自定义 JWT_SECRET（否则启动会被拒绝）"
        )
    try:
        yield
    finally:
        await close_checkpointer()
        close_pool()
        logger.info("Legal Flow API shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.2.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # 放在 CORS 之后注册 → 成为最外层中间件，最先执行，确保 corr_id 在所有处理前就绪。
    app.add_middleware(RequestLoggingMiddleware)

    app.include_router(health_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(sessions_routes.router)
    app.include_router(chat_routes.router)
    app.include_router(law_ingest_routes.router)
    app.include_router(contract_review_routes.router)
    return app


__all__ = ["create_app", "lifespan"]
