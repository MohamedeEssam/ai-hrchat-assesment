"""Search returns only relevant policies, and an empty plan from the model still searches."""
import time

from app.core.auth import RunContext
from app.data.runs import load_events_since
from app.data.search import search_policy

MAYA = RunContext("maya", "summit", "employee")
LEILA = RunContext("leila", "summit", "hr")
RAVI = RunContext("ravi", "pebble", "employee")


def _titles(ctx: RunContext, q: str) -> list[str]:
    return [r["title"] for r in search_policy(ctx, q)]


def test_specific_question_drops_unrelated_documents() -> None:
    assert _titles(RAVI, "late policy") == ["Late policy"]
    assert _titles(RAVI, "PTO") == ["Employee handbook"]
    assert _titles(LEILA, "CEO salary band") == ["CEO salary band"]
    assert "CEO salary band" not in _titles(LEILA, "what is the late policy?")


def test_small_talk_returns_nothing() -> None:
    assert search_policy(MAYA, "hello") == []


def test_general_question_still_lists_policies() -> None:
    assert set(_titles(RAVI, "what are the company policies?")) == {"Employee handbook", "Late policy"}


def test_results_do_not_expose_the_distance_column() -> None:
    row = search_policy(RAVI, "late policy")[0]
    assert set(row) == {"document_id", "title", "text"}


def test_empty_plan_falls_back_to_policy_search(client, scripted_manager) -> None:
    llm, _mgr = scripted_manager
    llm.structured_responses = [{"tasks": []}]
    llm.stream_responses = [["Pebble gives a warning only."]]

    res = client.post(
        "/runs", json={"message": "what is the late policy?"}, headers={"Authorization": "Bearer ravi-token"}
    )
    run_id = res.json()["run_id"]
    events: list[dict] = []
    for _ in range(100):
        events = load_events_since(RAVI, run_id, 0)
        if any(e["type"] in ("run.done", "run.error") for e in events):
            break
        time.sleep(0.05)
    calls = [e["payload"] for e in events if e["type"] == "tool.call"]
    assert calls and calls[0]["tool"] == "search_policy"
    assert "warning only" in llm.stream_prompts[-1]
