"""Runs one request as a job: plan -> run tasks (in parallel where possible) -> stream the answer.

A note sent while the job is working is stored, wakes the job and triggers a re-plan. Tasks whose
spec did not change keep their results; changed or removed tasks are cancelled and re-run.
Every step is written to ``run_events`` and streamed to the client over SSE.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.agent import guard, prompts
from app.agent.llm import LLM
from app.agent.plans import PlanTask, TaskResult, parse_plan, plan_schema
from app.agent.run_state import RunState
from app.agent.tools import TOOLS
from app.core.auth import RunContext
from app.core.config import settings
from app.data.audit import audit
from app.data.runs import append_event, append_note, load_events_since, load_inputs, update_run_status

__all__ = ["OrchestratorManager", "RunState"]

# How many times one job may re-plan because of notes before it stops accepting new ones.
MAX_PLAN_ROUNDS = 6


class OrchestratorManager:
    def __init__(self, llm: LLM):
        self.llm = llm
        self.runs: dict[str, RunState] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.tool_overrides: dict[str, Any] = {}
        # Hard limits so a hung model call can never leave a run "running" forever.
        self.plan_timeout = settings.llm_plan_timeout
        self.synth_timeout = settings.llm_answer_timeout

    def get_state(self, run_id: str) -> RunState | None:
        return self.runs.get(run_id)

    async def add_note(self, ctx: RunContext, run_id: str, text: str) -> int | None:
        """Attach a note to a job that is still working. Returns its seq, or None if the job has finished.

        The check and the insert run without an ``await`` in between, so on the single event loop a note
        can never slip in after the job decided to finish (it is refused instead, never silently lost).
        """
        state = self.runs.get(run_id)
        if state is None or not state.accepting_notes or state.ctx != ctx:
            return None
        seq = append_note(ctx, run_id, text)
        state.notes_event.set()
        await self.emit(state, "note.received", {"seq": seq, "text": text})
        return seq

    async def notify_note(self, run_id: str) -> None:
        st = self.runs.get(run_id)
        if st:
            st.notes_event.set()

    async def emit(self, state: RunState, event_type: str, payload: dict[str, Any]) -> None:
        append_event(state.ctx, state.run_id, event_type, payload)
        async with state.seq_cond:
            state.seq_cond.notify_all()

    async def start(self, run_id: str, ctx: RunContext, history: list[dict] | None = None) -> None:
        state = RunState(run_id=run_id, ctx=ctx, history=history or [])
        self.runs[run_id] = state
        task = asyncio.create_task(self._run(state))
        self.tasks[run_id] = task

    def _goal_and_notes(self, state: RunState) -> tuple[str, list[str]]:
        # The original request is always input #1, so re-planning never forgets it.
        inputs = load_inputs(state.ctx, state.run_id)
        goal = inputs[0]["text"] if inputs else ""
        return goal, [item["text"] for item in inputs[1:]]

    async def _make_plan(self, state: RunState) -> list[PlanTask]:
        goal, notes = self._goal_and_notes(state)
        other = guard.other_company_mentioned(state.ctx.company_id, [goal, *notes])
        if other:
            # Decided in code, not by the model, and recorded for review.
            audit(state.ctx, "request.other_company", {"company": other})
            reason = guard.refusal_text(state.ctx.company_id, other)
            return [PlanTask("refuse_1", "refuse", {"reason": reason}, [])]
        prompt = prompts.planner_prompt(
            goal, notes, state.history, state.current_plan, state.completed, company=state.ctx.company_id
        )
        payload: dict[str, Any] | None = None
        for attempt in (1, 2):
            try:
                payload = await asyncio.wait_for(
                    self.llm.structured(prompt, plan_schema()), timeout=self.plan_timeout
                )
                break
            except Exception as exc:  # timeout or transport error from the model provider
                await self.emit(
                    state,
                    "llm.retry" if attempt == 1 else "llm.failed",
                    {"stage": "plan", "attempt": attempt, "error": str(exc) or type(exc).__name__},
                )
                if attempt == 2:
                    raise RuntimeError("The AI model did not respond in time. Please try again.") from exc
        tasks = parse_plan(payload or {})
        if not tasks and not state.current_plan:
            # Small models sometimes return an empty plan. Searching the policies with the user's own
            # words is a safe default: it is read-only and scoped like every other search.
            tasks = [PlanTask("search_1", "search_policy", {"query": goal}, [])]
        return tasks

    async def _call_tool(self, state: RunState, task: PlanTask) -> TaskResult:
        # Overrides let tests replace a tool (e.g. with a slow fake) without touching the real ones.
        if task.task_type in self.tool_overrides:
            return await self.tool_overrides[task.task_type](state, task)
        tool = TOOLS.get(task.task_type)
        if tool is None:
            return TaskResult(task.task_id, "error", f"Unknown task type: {task.task_type}", None)
        return await tool(state, task, self.emit)

    def _same_task(self, a: PlanTask, b: PlanTask) -> bool:
        return a.task_type == b.task_type and a.args == b.args and a.depends_on == b.depends_on

    async def _apply_plan_update(self, state: RunState, new_plan: list[PlanTask]) -> dict[str, list[str]]:
        old = {t.task_id: t for t in state.current_plan}
        new = {t.task_id: t for t in new_plan}
        keep: list[str] = []
        add: list[str] = []
        modify: list[str] = []
        cancel: list[str] = []

        for task_id, prev in old.items():
            nxt = new.get(task_id)
            if not nxt:
                cancel.append(task_id)
            elif self._same_task(prev, nxt):
                keep.append(task_id)
            else:
                modify.append(task_id)
        for task_id in new:
            if task_id not in old:
                add.append(task_id)

        # Invalidate completed modified/canceled tasks so they can be rerun.
        for task_id in cancel + modify:
            state.completed.pop(task_id, None)
            state.completed_spec.pop(task_id, None)

        # Keep only still-valid completed tasks.
        for task_id, spec in list(state.completed_spec.items()):
            nxt = new.get(task_id)
            if not nxt or not self._same_task(spec, nxt):
                state.completed.pop(task_id, None)
                state.completed_spec.pop(task_id, None)

        state.current_plan = new_plan
        if old:
            await self.emit(
                state,
                "plan.updated",
                {
                    "tasks": [t.__dict__ for t in new_plan],
                    "diff": {"keep": keep, "add": add, "modify": modify, "cancel": cancel},
                },
            )
        else:
            await self.emit(state, "plan", {"tasks": [t.__dict__ for t in new_plan]})
        return {"keep": keep, "add": add, "modify": modify, "cancel": cancel}

    async def _run_tasks_with_notes(self, state: RunState) -> None:
        running: dict[str, asyncio.Task] = {}

        def launch_pending() -> None:
            for task in state.current_plan:
                if task.task_id in state.completed or task.task_id in running:
                    continue
                can_run = all(dep in state.completed for dep in task.depends_on)
                if not can_run:
                    continue
                running[task.task_id] = asyncio.create_task(self._call_tool(state, task))

        launch_pending()
        while running:
            note_wait = asyncio.create_task(state.notes_event.wait())
            done, pending = await asyncio.wait([*running.values(), note_wait], return_when=asyncio.FIRST_COMPLETED)

            if note_wait in done and state.notes_event.is_set():
                state.notes_event.clear()
                new_plan = await self._make_plan(state)
                diff = await self._apply_plan_update(state, new_plan)

                # Drop only tasks whose spec changed. Finished tasks that are still valid stay in
                # `running` so their result is recorded below (otherwise they would be re-run,
                # e.g. creating a duplicate proposal).
                for task_id in diff["cancel"] + diff["modify"]:
                    fut = running.pop(task_id, None)
                    if fut and not fut.done():
                        fut.cancel()
                launch_pending()

            for task_id, fut in list(running.items()):
                if fut.done():
                    running.pop(task_id, None)
                    if fut.cancelled():
                        continue
                    result = fut.result()
                    state.completed[task_id] = result
                    state.completed_spec[task_id] = next(t for t in state.current_plan if t.task_id == task_id)
                    await self.emit(state, "tool.result", {"task_id": task_id, "summary": result.summary})

            if not note_wait.done():
                note_wait.cancel()
            launch_pending()

    async def _synthesize(self, state: RunState) -> None:
        goal, notes = self._goal_and_notes(state)
        retrieved: list[str] = []
        for item in state.completed.values():
            if isinstance(item.data, list):
                retrieved.extend(str(row.get("text", "")) for row in item.data)
        prompt = prompts.answer_prompt(
            goal, notes, state.history, retrieved, state.completed, company=state.ctx.company_id
        )

        # Tells the client to start a fresh answer (a note can interrupt and restart this step).
        await self.emit(state, "answer.start", {})
        if state.current_plan and all(t.task_type == "refuse" for t in state.current_plan):
            # A refusal is sent as written; the model gets no chance to add anything to it.
            text = " ".join(r.summary for r in state.completed.values()) or "I cannot help with that."
            await self.emit(state, "answer.delta", {"delta": text})
            return
        try:
            async with asyncio.timeout(self.synth_timeout):
                async for token in self.llm.stream(prompt):
                    if state.notes_event.is_set():
                        return
                    await self.emit(state, "answer.delta", {"delta": token})
        except TimeoutError as exc:
            raise RuntimeError("The AI model took too long to answer. Please try again.") from exc

    async def _run(self, state: RunState) -> None:
        try:
            await self.emit(state, "run.started", {"run_id": state.run_id})
            for round_no in range(1, MAX_PLAN_ROUNDS + 1):
                if round_no == MAX_PLAN_ROUNDS:
                    # Last round: later notes are refused so the client starts a follow-up instead.
                    state.accepting_notes = False
                state.notes_event.clear()
                new_plan = await self._make_plan(state)
                await self._apply_plan_update(state, new_plan)
                await self._run_tasks_with_notes(state)
                await self._synthesize(state)
                if not state.notes_event.is_set():
                    # No await between this check and closing the door (see add_note).
                    state.accepting_notes = False
                    break
            await self.emit(state, "run.done", {"run_id": state.run_id})
            update_run_status(state.ctx, state.run_id, "done")
        except Exception as exc:
            state.accepting_notes = False
            await self.emit(state, "run.error", {"error": str(exc) or type(exc).__name__})
            update_run_status(state.ctx, state.run_id, "error")

    async def wait_for_events(self, ctx: RunContext, run_id: str, after_seq: int) -> list[dict]:
        state = self.runs.get(run_id)
        events = load_events_since(ctx, run_id, after_seq)
        if events:
            return events
        if state is None:
            return []
        async with state.seq_cond:
            try:
                await asyncio.wait_for(state.seq_cond.wait(), timeout=0.2)
            except asyncio.TimeoutError:
                return []
        return load_events_since(ctx, run_id, after_seq)
