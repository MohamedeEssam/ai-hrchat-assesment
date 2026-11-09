"""Shared FastAPI dependencies."""
import uuid

from fastapi import HTTPException, Request

from app.agent.orchestrator import OrchestratorManager
from app.core.auth import RunContext, get_run_context

__all__ = ["RunContext", "get_run_context", "get_manager", "parse_id"]


def parse_id(value: str, what: str) -> str:
    """Path ids are UUIDs; anything else cannot exist, so answer 404 instead of a DB error."""
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(status_code=404, detail=f"{what} not found")


def get_manager(request: Request) -> OrchestratorManager:
    # Stored on app.state so tests can swap in a manager with a scripted LLM.
    return request.app.state.manager
