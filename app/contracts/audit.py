"""合同审查结构化审计日志。"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.core.observability import get_correlation_id

audit_logger = logging.getLogger("app.contracts.audit")


def audit_event(event: str, **payload: Any) -> None:
    """写一条 JSONL 审计日志；日志失败不影响业务流程。"""
    record = {
        "ts": int(time.time() * 1000),
        "event": event,
        "correlation_id": get_correlation_id() or "-",
        **payload,
    }
    try:
        audit_logger.info(json.dumps(record, ensure_ascii=False, default=str))
    except Exception:
        logging.getLogger(__name__).exception("写合同审计日志失败 event=%s", event)


__all__ = ["audit_event"]
