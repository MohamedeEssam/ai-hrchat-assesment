"""In-memory state of one run (one user request plus any notes sent while it works)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.agent.plans import PlanTask, TaskResult
from app.core.auth import RunContext


@dataclass
class RunState:
    run_id: str
    ctx: RunContext
    # Set when a mid-run note arrives; the orchestrator re-plans when it sees it.
    notes_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Wakes SSE readers as soon as a new event is written.
    seq_cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    # False once the run has decided to finish; later notes are refused (HTTP 409) instead of lost.
    accepting_notes: bool = True
    completed: dict[str, TaskResult] = field(default_factory=dict)
    completed_spec: dict[str, PlanTask] = field(default_factory=dict)
    current_plan: list[PlanTask] = field(default_factory=list)
    # Planner task id -> proposal it created. A changed task replaces its own proposal, not another one.
    proposal_by_task: dict[str, str] = field(default_factory=dict)
    # Earlier turns of the same chat session (see data.runs.load_history).
    history: list[dict] = field(default_factory=list)
