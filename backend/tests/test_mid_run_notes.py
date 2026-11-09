"""A message sent while a job is working is an extra instruction for the SAME job."""
import asyncio
import threading
import time

from app.agent.plans import TaskResult
from app.core.auth import RunContext
from app.data.runs import append_event, create_run, load_events_since, load_history

MAYA = RunContext("maya", "summit", "employee")


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _wait(predicate, timeout: float = 5.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached in time")


def _types(run_id: str) -> list[str]:
    return [e["type"] for e in load_events_since(MAYA, run_id, 0)]


def test_note_during_run_joins_the_same_job_and_keeps_finished_work(client, scripted_manager) -> None:
    llm, mgr = scripted_manager
    search = {"task_id": "s1", "task_type": "search_policy", "args": {"query": "late"}, "depends_on": []}
    extra = {"task_id": "a1", "task_type": "answer_directly", "args": {"text": "short"}, "depends_on": []}
    llm.structured_responses = [{"tasks": [search]}, {"tasks": [search, extra]}]
    llm.stream_responses = [["Late 3 times means a pay cut."]]

    started, release = threading.Event(), threading.Event()
    calls: list[str] = []

    async def slow_search(state, task):
        calls.append(task.task_id)
        started.set()
        while not release.is_set():  # the app runs in another thread; poll without blocking its loop
            await asyncio.sleep(0.01)
        return TaskResult(task.task_id, "ok", "found", [])

    mgr.tool_overrides["search_policy"] = slow_search

    run_id = client.post("/runs", json={"message": "what is the late policy"}, headers=auth("maya-token")).json()["run_id"]
    assert started.wait(5)

    note = client.post(f"/runs/{run_id}/notes", json={"text": "keep it short"}, headers=auth("maya-token"))
    assert note.status_code == 200
    _wait(lambda: "plan.updated" in _types(run_id))
    release.set()
    _wait(lambda: "run.done" in _types(run_id))

    # One job, one answer: the note did not start a second run and the original goal was kept.
    assert _types(run_id).count("run.done") == 1
    assert "note.received" in _types(run_id)
    replan_prompt = llm.structured_prompts[-1]
    assert "what is the late policy" in replan_prompt and "keep it short" in replan_prompt
    # The unchanged search task was kept, not started again.
    assert calls == ["s1"]
    diff = [e for e in load_events_since(MAYA, run_id, 0) if e["type"] == "plan.updated"][-1]["payload"]["diff"]
    assert diff["keep"] == ["s1"] and diff["add"] == ["a1"]


def test_note_after_the_job_finished_is_refused_not_lost(client, scripted_manager) -> None:
    llm, _mgr = scripted_manager
    llm.structured_responses = [
        {"tasks": [{"task_id": "a", "task_type": "answer_directly", "args": {"text": "hi"}, "depends_on": []}]}
    ]
    llm.stream_responses = [["hello"]]

    run_id = client.post("/runs", json={"message": "hello"}, headers=auth("maya-token")).json()["run_id"]
    _wait(lambda: "run.done" in _types(run_id))

    late = client.post(f"/runs/{run_id}/notes", json={"text": "and also..."}, headers=auth("maya-token"))
    assert late.status_code == 409  # client then sends it as a follow-up message with history

    # Another user cannot add notes to Maya's run, even while guessing the id.
    other = client.post(f"/runs/{run_id}/notes", json={"text": "x"}, headers=auth("ravi-token"))
    assert other.status_code == 404


def test_history_keeps_only_the_final_answer_after_a_restart() -> None:
    run_id = create_run(MAYA, "question")
    append_event(MAYA, run_id, "answer.start", {})
    append_event(MAYA, run_id, "answer.delta", {"delta": "half-finished old answer"})
    append_event(MAYA, run_id, "answer.start", {})
    append_event(MAYA, run_id, "answer.delta", {"delta": "final answer"})

    [item] = load_history(MAYA, [run_id])
    assert item["answer"] == "final answer"


def test_login_rejects_unknown_users(client) -> None:
    assert client.post("/login", json={"username": "mallory"}).status_code == 401
    assert client.post("/login", json={"username": "maya"}).json() == {"token": "maya-token"}
