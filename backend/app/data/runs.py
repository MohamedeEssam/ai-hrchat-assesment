"""Runs (jobs), their inputs (request + notes) and their append-only event log."""
from __future__ import annotations

import json
import uuid
from typing import Any

from app.core.auth import RunContext
from app.core.db import fetch_all, fetch_one, tx_with_context


def create_run(ctx: RunContext, goal: str) -> str:
    run_id = str(uuid.uuid4())
    with tx_with_context(ctx) as conn:
        conn.execute(
            "INSERT INTO runs(id, company_id, user_id, goal, status) VALUES (%s,%s,%s,%s,'running')",
            (run_id, ctx.company_id, ctx.user_id, goal),
        )
        # Input #1 is always the original request, so re-planning never loses it.
        conn.execute(
            "INSERT INTO run_inputs(run_id, company_id, seq, text) VALUES (%s,%s,1,%s)",
            (run_id, ctx.company_id, goal),
        )
    return run_id


def get_run(ctx: RunContext, run_id: str) -> dict | None:
    with tx_with_context(ctx) as conn:
        return fetch_one(conn, "SELECT * FROM runs WHERE id=%s", (run_id,))


def update_run_status(ctx: RunContext, run_id: str, status: str) -> None:
    with tx_with_context(ctx) as conn:
        conn.execute("UPDATE runs SET status=%s WHERE id=%s", (status, run_id))


def append_note(ctx: RunContext, run_id: str, text: str) -> int:
    with tx_with_context(ctx) as conn:
        row = fetch_one(conn, "SELECT COALESCE(max(seq),0) AS m FROM run_inputs WHERE run_id=%s", (run_id,))
        seq = int(row["m"]) + 1
        conn.execute(
            "INSERT INTO run_inputs(run_id, company_id, seq, text) VALUES (%s,%s,%s,%s)",
            (run_id, ctx.company_id, seq, text),
        )
        return seq


def load_inputs(ctx: RunContext, run_id: str) -> list[dict]:
    with tx_with_context(ctx) as conn:
        return fetch_all(conn, "SELECT seq,text FROM run_inputs WHERE run_id=%s ORDER BY seq", (run_id,))


def append_event(ctx: RunContext, run_id: str, event_type: str, payload: dict[str, Any]) -> int:
    with tx_with_context(ctx) as conn:
        row = fetch_one(conn, "SELECT COALESCE(max(seq),0) AS m FROM run_events WHERE run_id=%s", (run_id,))
        seq = int(row["m"]) + 1
        conn.execute(
            "INSERT INTO run_events(run_id, company_id, seq, type, payload) VALUES (%s,%s,%s,%s,%s::jsonb)",
            (run_id, ctx.company_id, seq, event_type, json.dumps(payload)),
        )
        return seq


def load_events_since(ctx: RunContext, run_id: str, last_event_id: int) -> list[dict]:
    with tx_with_context(ctx) as conn:
        return fetch_all(
            conn,
            "SELECT seq,type,payload FROM run_events WHERE run_id=%s AND seq>%s ORDER BY seq",
            (run_id, last_event_id),
        )


def load_history(ctx: RunContext, run_ids: list[str]) -> list[dict]:
    """Earlier turns of the caller's own conversation (oldest first).

    Only runs owned by the calling user are returned; RLS also enforces tenant scope.
    """
    if not run_ids:
        return []
    history: list[dict] = []
    with tx_with_context(ctx) as conn:
        runs = fetch_all(
            conn,
            "SELECT id FROM runs WHERE id::text = ANY(%s) AND user_id=%s ORDER BY created_at",
            (run_ids, ctx.user_id),
        )
        for r in runs:
            rid = str(r["id"])
            inputs = fetch_all(conn, "SELECT text FROM run_inputs WHERE run_id=%s ORDER BY seq", (rid,))
            events = fetch_all(
                conn,
                "SELECT type, payload FROM run_events WHERE run_id=%s "
                "AND type IN ('answer.start','answer.delta') ORDER BY seq",
                (rid,),
            )
            # A note can restart the answer; keep only the text after the last answer.start.
            answer_parts: list[str] = []
            for e in events:
                if e["type"] == "answer.start":
                    answer_parts = []
                else:
                    answer_parts.append(str((e["payload"] or {}).get("delta", "")))
            proposals = fetch_all(
                conn,
                "SELECT id, status, payload FROM proposals WHERE run_id=%s ORDER BY created_at",
                (rid,),
            )
            history.append(
                {
                    "run_id": rid,
                    "inputs": [i["text"] for i in inputs],
                    "answer": "".join(answer_parts),
                    "proposals": [
                        {"id": str(p["id"]), "status": p["status"], "payload": p["payload"]} for p in proposals
                    ],
                }
            )
    return history
