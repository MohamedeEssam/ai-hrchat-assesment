"""Bad ids and other people's runs get clean 4xx answers, never a 500 or an empty stream."""
from app.core.auth import RunContext
from app.data.runs import create_run

MAYA = RunContext("maya", "summit", "employee")


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_malformed_ids_are_404(client, scripted_manager) -> None:
    h = auth("maya-token")
    assert client.get("/runs/not-a-uuid", headers=h).status_code == 404
    assert client.get("/runs/not-a-uuid/events?once=true", headers=h).status_code == 404
    assert client.post("/runs/not-a-uuid/notes", json={"text": "x"}, headers=h).status_code == 404
    assert client.post("/proposals/not-a-uuid/confirm", headers=h).status_code == 404
    assert client.post("/proposals/not-a-uuid/reject", headers=h).status_code == 404


def test_unknown_proposal_confirm_is_404(client, scripted_manager) -> None:
    res = client.post("/proposals/00000000-0000-0000-0000-000000000000/confirm", headers=auth("maya-token"))
    assert res.status_code == 404


def test_event_stream_of_another_tenants_run_is_404(client, scripted_manager) -> None:
    run_id = create_run(MAYA, "private")
    assert client.get(f"/runs/{run_id}/events?once=true", headers=auth("ravi-token")).status_code == 404


def test_bad_last_event_id_is_400(client, scripted_manager) -> None:
    run_id = create_run(MAYA, "hello")
    res = client.get(
        f"/runs/{run_id}/events?once=true",
        headers={**auth("maya-token"), "Last-Event-ID": "abc"},
    )
    assert res.status_code == 400
