"""Changing a leave request that is already confirmed must charge only the difference, never twice."""
import asyncio

import pytest

from app.agent.plans import TaskResult
from app.core.auth import RunContext
from app.data.leave import (
    confirm_proposal,
    create_proposal,
    get_leave_balance,
    list_my_proposals,
    reject_proposal,
)
from app.data.runs import append_note, create_run, load_events_since

MAYA = RunContext("maya", "summit", "employee")


def _status(proposal_id: str) -> str:
    return next(p["status"] for p in list_my_proposals(MAYA, limit=500) if p["id"] == proposal_id)


def test_amending_a_confirmed_booking_charges_only_the_difference() -> None:
    start = get_leave_balance(MAYA)
    run_id = create_run(MAYA, "book 2 days")
    two = create_proposal(MAYA, run_id, {"days": 2, "dates": ["2026-11-09", "2026-11-10"]})
    assert confirm_proposal(MAYA, two["id"])[0]
    assert get_leave_balance(MAYA) == start - 2

    three = create_proposal(
        MAYA, run_id, {"days": 3, "dates": ["2026-11-09", "2026-11-10", "2026-11-11"]}, supersedes_id=two["id"]
    )
    # Proposing changes nothing: the confirmed booking stays confirmed until the change is confirmed.
    assert three["replaces"]["id"] == two["id"] and three["net_days"] == 1
    assert _status(two["id"]) == "confirmed"
    assert get_leave_balance(MAYA) == start - 2

    assert confirm_proposal(MAYA, three["id"])[0]
    assert get_leave_balance(MAYA) == start - 3  # not start - 5
    assert _status(two["id"]) == "amended"

    # Confirming again is still a no-op.
    assert confirm_proposal(MAYA, three["id"]) == (True, "already confirmed")
    assert get_leave_balance(MAYA) == start - 3


def test_rejecting_the_change_keeps_the_original_booking() -> None:
    start = get_leave_balance(MAYA)
    run_id = create_run(MAYA, "book 1 day")
    one = create_proposal(MAYA, run_id, {"days": 1, "dates": ["2026-12-01"]})
    confirm_proposal(MAYA, one["id"])
    change = create_proposal(MAYA, run_id, {"days": 2, "dates": ["2026-12-01", "2026-12-02"]}, supersedes_id=one["id"])

    assert reject_proposal(MAYA, change["id"])
    assert _status(one["id"]) == "confirmed"
    assert get_leave_balance(MAYA) == start - 1


@pytest.mark.asyncio
async def test_note_after_confirm_in_the_same_run_amends_instead_of_double_charging(scripted_manager) -> None:
    """Reproduces the bug: confirm 2 days, then 'make it 3 days' in the same run used to charge 2 + 3."""
    llm, mgr = scripted_manager
    two = {"task_id": "p", "task_type": "propose_leave", "args": {"days": 2, "dates": ["2027-01-04", "2027-01-05"]}, "depends_on": []}
    three = {**two, "args": {"days": 3, "dates": ["2027-01-04", "2027-01-05", "2027-01-06"]}}
    search = {"task_id": "s", "task_type": "search_policy", "args": {"query": "leave"}, "depends_on": []}
    llm.structured_responses = [{"tasks": [two, search]}, {"tasks": [three, search]}]
    llm.stream_responses = [["ok"]]

    release = asyncio.Event()

    async def slow_search(state, task):  # keeps the run working while the user confirms
        await release.wait()
        return TaskResult(task.task_id, "ok", "found", [])

    mgr.tool_overrides["search_policy"] = slow_search

    start = get_leave_balance(MAYA)
    run_id = create_run(MAYA, "book 2 days and tell me the policy")
    await mgr.start(run_id, MAYA)

    def proposals() -> list[dict]:
        return [e["payload"] for e in load_events_since(MAYA, run_id, 0) if e["type"] == "proposal"]

    for _ in range(100):
        if proposals():
            break
        await asyncio.sleep(0.02)
    first = proposals()[0]
    assert confirm_proposal(MAYA, first["id"])[0]
    assert get_leave_balance(MAYA) == start - 2

    append_note(MAYA, run_id, "make it 3 days")
    await mgr.notify_note(run_id)
    for _ in range(100):
        if len(proposals()) == 2:
            break
        await asyncio.sleep(0.02)
    release.set()
    await asyncio.wait_for(mgr.tasks[run_id], timeout=5)

    second = proposals()[-1]
    assert second["replaces"]["id"] == first["id"]
    assert confirm_proposal(MAYA, second["id"])[0]
    assert get_leave_balance(MAYA) == start - 3
    assert _status(first["id"]) == "amended"
