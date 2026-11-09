import asyncio
import json

import pytest

from app.agent.plans import TaskResult
from app.agent.run_state import RunState
from app.core.auth import RunContext
from app.data.audit import load_audit
from app.data.leave import create_proposal, get_leave_balance
from app.data.runs import append_note, create_run, load_events_since
from app.data.search import search_policy


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_propose_and_confirm_flow(client, scripted_manager) -> None:
    llm, _mgr = scripted_manager
    llm.structured_responses = [
        {
            "tasks": [
                {"task_id": "b1", "task_type": "get_leave_balance", "args": {}, "depends_on": []},
                {
                    "task_id": "p1",
                    "task_type": "propose_leave",
                    "args": {"days": 2, "dates": ["2026-11-10", "2026-11-11"]},
                    "depends_on": ["b1"],
                },
            ]
        }
    ]
    llm.stream_responses = [["I prepared a proposal."]]

    before = client.get("/policies/search?q=anything", headers=auth("maya-token"))
    assert before.status_code == 200

    res = client.post("/runs", json={"message": "book 2 days next week"}, headers=auth("maya-token"))
    assert res.status_code == 200
    run_id = res.json()["run_id"]

    # wait for proposal event
    for _ in range(40):
        events = load_events_since(RunContext("maya", "summit", "employee"), run_id, 0)
        proposal_events = [e for e in events if e["type"] == "proposal"]
        if proposal_events:
            proposal = proposal_events[-1]["payload"]
            break
        asyncio.run(asyncio.sleep(0.05))
    else:
        raise AssertionError("proposal event not found")

    # Propose must not deduct yet
    assert get_leave_balance(RunContext("maya", "summit", "employee")) == 12

    proposal_id = proposal["id"]
    ok = client.post(f"/proposals/{proposal_id}/confirm", headers=auth("maya-token"))
    assert ok.status_code == 200
    assert get_leave_balance(RunContext("maya", "summit", "employee")) == 10

    # idempotent confirm
    ok2 = client.post(f"/proposals/{proposal_id}/confirm", headers=auth("maya-token"))
    assert ok2.status_code == 200
    assert get_leave_balance(RunContext("maya", "summit", "employee")) == 10

    # other user cannot confirm (looks the same as "does not exist")
    bad = client.post(f"/proposals/{proposal_id}/confirm", headers=auth("leila-token"))
    assert bad.status_code == 404


def test_injection_scope_override_is_audited_and_blocked(client, scripted_manager) -> None:
    llm, _mgr = scripted_manager
    llm.structured_responses = [
        {
            "tasks": [
                {
                    "task_id": "s1",
                    "task_type": "search_policy",
                    "args": {"query": "Pebble handbook and CEO salary", "company_id": "pebble"},
                    "depends_on": [],
                }
            ]
        }
    ]
    llm.stream_responses = [["I can only share permitted info."]]

    poisoned = search_policy(RunContext("maya", "summit", "employee"), "Ignore previous instructions")
    assert any(row["title"] == "Poisoned note" for row in poisoned)

    res = client.post("/runs", json={"message": "what is late policy"}, headers=auth("maya-token"))
    assert res.status_code == 200
    run_id = res.json()["run_id"]

    for _ in range(50):
        events = load_events_since(RunContext("maya", "summit", "employee"), run_id, 0)
        if any(e["type"] == "run.done" for e in events):
            break
        asyncio.run(asyncio.sleep(0.05))

    events = load_events_since(RunContext("maya", "summit", "employee"), run_id, 0)
    all_payload = json.dumps([e["payload"] for e in events])
    assert "Pebble handbook:" not in all_payload
    assert "CEO salary band: confidential" not in all_payload

    audit = load_audit(RunContext("maya", "summit", "employee"))
    assert any(item["action"] == "tool.scope_override_attempt" for item in audit)


@pytest.mark.asyncio
async def test_mid_run_note_replanning_keeps_completed_and_cancels_obsolete(scripted_manager) -> None:
    llm, mgr = scripted_manager

    llm.structured_responses = [
        {
            "tasks": [
                {"task_id": "t_balance", "task_type": "get_leave_balance", "args": {}, "depends_on": []},
                {"task_id": "t_policy", "task_type": "search_policy", "args": {"query": "late"}, "depends_on": []},
                {"task_id": "t_prop", "task_type": "propose_leave", "args": {"days": 2, "dates": ["A"]}, "depends_on": ["t_balance"]},
            ]
        },
        {
            "tasks": [
                {"task_id": "t_balance", "task_type": "get_leave_balance", "args": {}, "depends_on": []},
                {"task_id": "t_policy", "task_type": "search_policy", "args": {"query": "late"}, "depends_on": []},
                {"task_id": "t_prop", "task_type": "propose_leave", "args": {"days": 3, "dates": ["B"]}, "depends_on": ["t_balance"]},
            ]
        },
    ]
    llm.stream_responses = [["ok"], ["ok"]]

    started = asyncio.Event()
    unblock = asyncio.Event()

    async def slow_search(state: RunState, task):
        started.set()
        await unblock.wait()
        return TaskResult(task.task_id, "ok", "done", [])

    mgr.tool_overrides["search_policy"] = slow_search

    ctx = RunContext("maya", "summit", "employee")
    balance_before = get_leave_balance(ctx)
    run_id = create_run(ctx, "book leave and tell policy")
    await mgr.start(run_id, ctx)

    await asyncio.wait_for(started.wait(), timeout=2)
    # note arrives while one task is running
    append_note(ctx, run_id, "make it 3 days")
    await mgr.notify_note(run_id)
    unblock.set()

    await asyncio.wait_for(mgr.tasks[run_id], timeout=5)

    # original goal included in prompts
    assert llm.structured_prompts
    assert all("book leave and tell policy" in p for p in llm.structured_prompts)

    events = load_events_since(ctx, run_id, 0)
    # The finished balance task was kept, not re-run after the note.
    balance_calls = [e for e in events if e["type"] == "tool.call" and e["payload"].get("tool") == "get_leave_balance"]
    assert len(balance_calls) == 1
    # The latest proposal reflects the note (3 days), and nothing was deducted yet.
    proposals = [e["payload"] for e in events if e["type"] == "proposal"]
    assert proposals and proposals[-1]["payload"]["days"] == 3
    assert get_leave_balance(ctx) == balance_before


def test_sse_replay_order_and_resume(client, scripted_manager) -> None:
    llm, _mgr = scripted_manager
    llm.structured_responses = [{"tasks": [{"task_id": "a", "task_type": "answer_directly", "args": {"text": "hi"}, "depends_on": []}]}]
    llm.stream_responses = [["hello"]]

    run_id = client.post("/runs", json={"message": "hello"}, headers=auth("maya-token")).json()["run_id"]

    first = client.get(f"/runs/{run_id}/events?once=true", headers=auth("maya-token"))
    assert first.status_code == 200
    seen_ids = []
    for line in first.text.splitlines():
        if line.startswith("id: "):
            seen_ids.append(int(line.split(": ", 1)[1]))

    assert seen_ids == sorted(seen_ids)
    last_id = seen_ids[-1]

    second = client.get(
        f"/runs/{run_id}/events?once=true",
        headers={**auth("maya-token"), "Last-Event-ID": str(last_id)},
    )
    assert second.status_code == 200
    resumed_ids = [int(line.split(": ", 1)[1]) for line in second.text.splitlines() if line.startswith("id: ")]
    if resumed_ids:
        assert all(item > last_id for item in resumed_ids)


@pytest.mark.asyncio
async def test_parallel_tasks_overlap(scripted_manager) -> None:
    llm, mgr = scripted_manager
    llm.structured_responses = [
        {
            "tasks": [
                {"task_id": "x", "task_type": "search_policy", "args": {"query": "a"}, "depends_on": []},
                {"task_id": "y", "task_type": "get_leave_balance", "args": {}, "depends_on": []},
            ]
        }
    ]
    llm.stream_responses = [["done"]]

    started = []
    gate = asyncio.Event()

    async def fake_search(state: RunState, task):
        started.append(task.task_id)
        await gate.wait()
        return TaskResult(task.task_id, "ok", "s", [])

    async def fake_balance(state: RunState, task):
        started.append(task.task_id)
        await gate.wait()
        return TaskResult(task.task_id, "ok", "b", {"days": 1})

    mgr.tool_overrides["search_policy"] = fake_search
    mgr.tool_overrides["get_leave_balance"] = fake_balance

    ctx = RunContext("maya", "summit", "employee")
    run_id = create_run(ctx, "do both")
    await mgr.start(run_id, ctx)

    for _ in range(50):
        if len(started) == 2:
            break
        await asyncio.sleep(0.02)
    assert set(started) == {"x", "y"}
    gate.set()
    await asyncio.wait_for(mgr.tasks[run_id], timeout=5)


def test_superseded_proposal_cannot_be_confirmed(client, scripted_manager) -> None:
    ctx = RunContext("maya", "summit", "employee")
    run_id = create_run(ctx, "book leave")
    p1 = create_proposal(ctx, run_id, {"days": 2, "dates": ["A"]}, supersedes_id=None)
    _p2 = create_proposal(ctx, run_id, {"days": 3, "dates": ["B"]}, supersedes_id=p1["id"])
    old_id = p1["id"]
    resp = client.post(f"/proposals/{old_id}/confirm", headers=auth("maya-token"))
    assert resp.status_code == 409
