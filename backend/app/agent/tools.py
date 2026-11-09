"""The tools the planner can schedule.

Every tool reads tenant, user and role from ``state.ctx`` (set from the auth token), never from
model-supplied arguments, and every database call runs under RLS. Tools that change data only create
a *proposal*; the actual write happens in ``repo.confirm_proposal`` after the user presses Confirm.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
import uuid
from datetime import date, timedelta
from typing import Any

from app.agent.plans import PlanTask, TaskResult
from app.agent.run_state import RunState
from app.data.audit import audit
from app.data.leave import create_proposal, get_leave_balance
from app.data.search import search_policy

Emit = Callable[[RunState, str, dict[str, Any]], Awaitable[None]]
Tool = Callable[[RunState, PlanTask, Emit], Awaitable[TaskResult]]

SCOPE_KEYS = ("company_id", "user_id", "role", "tenant", "company")


def _parse_iso(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def build_leave_payload(args: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Normalise planner args into ``{days, dates}``; refuse empty or zero-day requests."""
    dates = [str(d) for d in (args.get("dates") or []) if d]
    try:
        days = int(args.get("days") or 0)
    except (TypeError, ValueError):
        days = 0

    start = _parse_iso(args.get("start_date"))
    end = _parse_iso(args.get("end_date"))
    if not dates and start:
        if end is None:
            end = start + timedelta(days=max(days, 1) - 1)
        if end >= start and (end - start).days < 60:
            dates = [(start + timedelta(n)).isoformat() for n in range((end - start).days + 1)]

    if days <= 0 and dates:
        days = len(dates)
    if days <= 0:
        return None, "Cannot create a leave proposal: the number of days or the dates are missing. Ask the user for them."
    return {"days": days, "dates": dates}, None


async def search_policy_tool(state: RunState, task: PlanTask, emit: Emit) -> TaskResult:
    args = dict(task.args)
    injected = {k: args.pop(k) for k in SCOPE_KEYS if k in args}
    if injected:
        # Scope comes from the token only. Record the attempt and drop the arguments.
        audit(state.ctx, "tool.scope_override_attempt", {"tool": "search_policy", "args": {**args, **injected}})
    query = str(args.get("query", ""))
    await emit(state, "tool.call", {"tool": "search_policy", "task_id": task.task_id, "args": {"query": query}})
    rows = search_policy(state.ctx, query)
    await emit(state, "tool.result", {"tool": "search_policy", "task_id": task.task_id, "count": len(rows)})
    if not rows:
        return TaskResult(task.task_id, "ok", "No policy document matches this question.", [])
    # Summaries are trusted prompt text, so they never contain anything taken from a document
    # (not even its title). Document text only reaches the answer prompt inside the nonce block.
    return TaskResult(task.task_id, "ok", f"Found {len(rows)} relevant policy chunk(s).", rows)


async def leave_balance_tool(state: RunState, task: PlanTask, emit: Emit) -> TaskResult:
    await emit(state, "tool.call", {"tool": "get_leave_balance", "task_id": task.task_id, "args": {}})
    days = get_leave_balance(state.ctx)
    await emit(state, "tool.result", {"tool": "get_leave_balance", "task_id": task.task_id, "days": days})
    return TaskResult(task.task_id, "ok", f"Leave balance is {days} days.", {"days": days})


def _proposal_to_replace(state: RunState, task: PlanTask) -> str | None:
    """Which earlier proposal (if any) this propose_leave task replaces.

    1. An explicit ``args.replaces`` id from the planner (a change to a request from an earlier turn).
    2. The proposal this same task created before a note changed it ("make it 3 days").
    3. A proposal whose task the re-plan dropped (the planner renamed the task).
    A new, extra task with no link creates an independent request.
    """
    explicit = str(task.args.get("replaces") or "").strip()
    if explicit:
        try:
            return str(uuid.UUID(explicit))
        except ValueError:
            pass
    if task.task_id in state.proposal_by_task:
        return state.proposal_by_task[task.task_id]
    live = {t.task_id for t in state.current_plan}
    orphans = [tid for tid in state.proposal_by_task if tid not in live]
    if orphans:
        return state.proposal_by_task.pop(orphans[-1])
    return None


async def propose_leave_tool(state: RunState, task: PlanTask, emit: Emit) -> TaskResult:
    payload, error = build_leave_payload(task.args)
    if error or payload is None:
        return TaskResult(task.task_id, "error", error or "Invalid leave request.", None)
    # Pending proposals are superseded now; a confirmed one is only replaced when this one is confirmed.
    proposal = create_proposal(state.ctx, state.run_id, payload, supersedes_id=_proposal_to_replace(state, task))
    state.proposal_by_task[task.task_id] = proposal["id"]
    await emit(state, "proposal", proposal)
    summary = f"Created proposal {proposal['id']} for {payload['days']} day(s) on {payload['dates']}. "
    replaced = proposal.get("replaces")
    if replaced and replaced["status"] == "confirmed":
        summary += (
            f"It changes the already confirmed booking of {replaced['payload'].get('days')} day(s); "
            f"on Confirm only the difference ({proposal['net_days']} day(s)) is charged. "
        )
    summary += "Nothing is saved yet: the user must press Confirm on the proposal card."
    return TaskResult(task.task_id, "ok", summary, proposal)


async def answer_directly_tool(state: RunState, task: PlanTask, emit: Emit) -> TaskResult:
    text = task.args.get("text") or task.args.get("response") or task.args.get("message") or "Hello"
    return TaskResult(task.task_id, "ok", str(text), None)


async def refuse_tool(state: RunState, task: PlanTask, emit: Emit) -> TaskResult:
    return TaskResult(task.task_id, "ok", str(task.args.get("reason", "I cannot help with that.")), None)


TOOLS: dict[str, Tool] = {
    "search_policy": search_policy_tool,
    "get_leave_balance": leave_balance_tool,
    "propose_leave": propose_leave_tool,
    "answer_directly": answer_directly_tool,
    "refuse": refuse_tool,
}
