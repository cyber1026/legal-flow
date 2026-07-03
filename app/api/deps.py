"""Shared FastAPI dependency providers (singletons / lazy resources)."""

from __future__ import annotations

from app.agents.supervisor import get_supervisor_agent

__all__ = ["get_supervisor_agent"]
