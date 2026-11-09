import time

from app.agent.prompts import history_block as _history_block
from app.agent.tools import build_leave_payload as _build_leave_payload
from app.core.auth import RunContext
from app.data.leave import create_proposal
from app.data.runs import create_run, load_events_since

MAYA = RunContext("maya", "summit", "employee")


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _wait_done(ctx: RunContext, run_id: str) -> list[dict]:
    for _ in range(100):
        events = load_events_since(ctx, run_id, 0)
        if any(e["type"] in ("run.done", "run.error") for e in events):
            return events
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def _answer_plan(text: str) -> dict:
    return {"tasks": [{"task_id": "a", "task_type": "answer_directly", "args": {"text": text}, "depends_on": []}]}


def test_follow_up_run_sees_earlier_messages_and_answers(client, scripted_manager) -> None:
    llm, _mgr = scripted_manager
    llm.structured_responses = [_answer_plan("first"), _answer_plan("second")]
    llm.stream_responses = [["Your balance is twelve days."], ["ok"]]

    first = client.post("/runs", json={"message": "how many leave days do I have"}, headers=auth("maya-token"))
    first_id = first.json()["run_id"]
    _wait_done(MAYA, first_id)

    second = client.post(
        "/runs",
        json={"message": "submit it", "context_run_ids": [first_id]},
        headers=auth("maya-token"),
    )
    _wait_done(MAYA, second.json()["run_id"])

    planner_prompt = llm.structured_prompts[-1]
    assert "how many leave days do I have" in planner_prompt
    assert "Your balance is twelve days." in planner_prompt
    assert "submit it" in planner_prompt
    assert "Today is " in planner_prompt


def test_history_is_limited_to_the_callers_own_runs(client, scripted_manager) -> None:
    llm, _mgr = scripted_manager
    llm.structured_responses = [_answer_plan("a"), _answer_plan("b")]
    llm.stream_responses = [["secret maya answer"], ["ok"]]

    maya_run = client.post("/runs", json={"message": "maya private question"}, headers=auth("maya-token"))
    maya_run_id = maya_run.json()["run_id"]
    _wait_done(MAYA, maya_run_id)

    leila_ctx = RunContext("leila", "summit", "hr")
    other = client.post(
        "/runs",
        json={"message": "hello", "context_run_ids": [maya_run_id]},
        headers=auth("leila-token"),
    )
    _wait_done(leila_ctx, other.json()["run_id"])

    leila_prompt = llm.structured_prompts[-1]
    assert "maya private question" not in leila_prompt
    assert "secret maya answer" not in leila_prompt


def test_invalid_context_run_id_is_rejected(client, scripted_manager) -> None:
    res = client.post(
        "/runs",
        json={"message": "hi", "context_run_ids": ["not-a-uuid"]},
        headers=auth("maya-token"),
    )
    assert res.status_code == 400


def test_leave_payload_uses_start_and_end_date() -> None:
    payload, error = _build_leave_payload({"days": 2, "start_date": "2026-11-16", "end_date": "2026-11-17"})
    assert error is None
    assert payload == {"days": 2, "dates": ["2026-11-16", "2026-11-17"]}


def test_leave_payload_derives_days_from_dates_and_end_from_days() -> None:
    payload, _ = _build_leave_payload({"dates": ["2026-11-16", "2026-11-17", "2026-11-18"]})
    assert payload == {"days": 3, "dates": ["2026-11-16", "2026-11-17", "2026-11-18"]}

    payload, _ = _build_leave_payload({"days": 2, "start_date": "2026-11-16"})
    assert payload == {"days": 2, "dates": ["2026-11-16", "2026-11-17"]}


def test_zero_day_leave_is_refused() -> None:
    payload, error = _build_leave_payload({})
    assert payload is None
    assert error


def test_zero_day_plan_creates_no_proposal(client, scripted_manager, conn) -> None:
    llm, _mgr = scripted_manager
    llm.structured_responses = [
        {"tasks": [{"task_id": "p", "task_type": "propose_leave", "args": {}, "depends_on": []}]}
    ]
    llm.stream_responses = [["Which dates?"]]

    run_id = client.post("/runs", json={"message": "book leave"}, headers=auth("maya-token")).json()["run_id"]
    events = _wait_done(MAYA, run_id)
    assert not any(e["type"] == "proposal" for e in events)


def test_answer_directly_accepts_response_key(client, scripted_manager) -> None:
    llm, _mgr = scripted_manager
    llm.structured_responses = [
        {
            "tasks": [
                {"task_id": "a", "task_type": "answer_directly", "args": {"response": "custom reply"}, "depends_on": []}
            ]
        }
    ]
    llm.stream_responses = [["done"]]

    run_id = client.post("/runs", json={"message": "hi"}, headers=auth("maya-token")).json()["run_id"]
    events = _wait_done(MAYA, run_id)
    summaries = [e["payload"].get("summary") for e in events if e["type"] == "tool.result"]
    assert "custom reply" in summaries


def test_history_block_states_the_current_proposal_status() -> None:
    history = [
        {
            "run_id": "r1",
            "inputs": ["book 2 days"],
            "answer": "Press Confirm on the proposal card.",
            "proposals": [{"id": "p1", "status": "confirmed", "payload": {"days": 2, "dates": ["2026-11-09", "2026-11-10"]}}],
        }
    ]
    block = _history_block(history)
    assert "CONFIRMED" in block
    assert "2 day(s)" in block
    assert "2026-11-09" in block


def test_me_leave_returns_only_the_callers_own_proposals(client, scripted_manager) -> None:
    maya_run = create_run(MAYA, "book leave")
    create_proposal(MAYA, maya_run, {"days": 1, "dates": ["2026-12-01"]})

    mine = client.get("/me/leave", headers=auth("maya-token"))
    assert mine.status_code == 200
    body = mine.json()
    assert body["balance"] == 12
    assert any(p["payload"]["dates"] == ["2026-12-01"] for p in body["proposals"])

    # Another user in the same company and a user in another company see none of it.
    for token in ("leila-token", "ravi-token"):
        other = client.get("/me/leave", headers=auth(token)).json()
        assert not any(p["payload"].get("dates") == ["2026-12-01"] for p in other["proposals"])

    assert client.get("/me/leave").status_code == 401


def test_hung_model_call_ends_the_run_with_an_error(client, scripted_manager) -> None:
    import asyncio as _asyncio

    llm, mgr = scripted_manager
    mgr.plan_timeout = 0.2

    async def hang(prompt, schema):
        await _asyncio.sleep(30)
        return {}

    llm.structured = hang  # type: ignore[method-assign]

    run_id = client.post("/runs", json={"message": "hello"}, headers=auth("maya-token")).json()["run_id"]
    events = _wait_done(MAYA, run_id)
    assert any(e["type"] == "llm.retry" for e in events)
    error = [e for e in events if e["type"] == "run.error"]
    assert error and "did not respond" in error[0]["payload"]["error"]
