"""Shared datetime helpers for DB-backed stores."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def to_utc_datetime(value: Any) -> datetime:
    """Parse DB/API datetimes; naive values are treated as UTC."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError(f"Unexpected datetime value: {value!r}")
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
