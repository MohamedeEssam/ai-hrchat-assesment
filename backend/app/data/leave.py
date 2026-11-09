"""Leave balances and leave proposals (propose first, write only on Confirm).

Proposal statuses:
- ``pending``: waiting for the user. Nothing has changed yet.
- ``confirmed``: the days were deducted.
- ``rejected`` / ``expired``: never applied.
- ``superseded``: a pending proposal replaced by a newer one before it was confirmed.
- ``amended``: a confirmed booking replaced by a newer confirmed one. Its days were refunded when the
  newer one was confirmed, so the same dates are never charged twice.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.auth import RunContext
from app.core.db import fetch_all, fetch_one, tx_with_context

PROPOSAL_TTL = timedelta(minutes=20)


def _days(payload: dict | None) -> int:
    return int((payload or {}).get("days", 0) or 0)


def get_leave_balance(ctx: RunContext) -> int:
    with tx_with_context(ctx) as conn:
        row = fetch_one(
            conn,
            "SELECT days FROM leave_balances WHERE company_id=%s AND user_id=%s",
            (ctx.company_id, ctx.user_id),
        )
        return int(row["days"] if row else 0)


def create_proposal(ctx: RunContext, run_id: str, payload: dict[str, Any], supersedes_id: str | None = None) -> dict:
    """Store a pending change. Never touches the balance.

    ``supersedes_id`` is the caller's earlier proposal that this one replaces:
    - if it is still pending it is marked ``superseded`` now (it can no longer be confirmed);
    - if it is already confirmed it stays confirmed until *this* proposal is confirmed, and then only the
      difference in days is charged (see ``confirm_proposal``).
    Anything else (unknown id, another user's proposal, already replaced) is ignored.
    """
    proposal_id = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + PROPOSAL_TTL
    replaces: dict[str, Any] | None = None
    with tx_with_context(ctx) as conn:
        if supersedes_id:
            old = conn.execute(
                "SELECT id, status, payload FROM proposals WHERE id=%s AND owner_user_id=%s FOR UPDATE",
                (supersedes_id, ctx.user_id),
            ).fetchone()
            if old is None or old["status"] not in ("pending", "confirmed"):
                supersedes_id = None
            else:
                if old["status"] == "pending":
                    conn.execute("UPDATE proposals SET status='superseded' WHERE id=%s", (supersedes_id,))
                replaces = {"id": str(old["id"]), "status": old["status"], "payload": old["payload"]}
        conn.execute(
            """
            INSERT INTO proposals(id, run_id, company_id, owner_user_id, kind, payload, status, supersedes_id, expires_at)
            VALUES (%s,%s,%s,%s,'leave_booking',%s::jsonb,'pending',%s,%s)
            """,
            (proposal_id, run_id, ctx.company_id, ctx.user_id, json.dumps(payload), supersedes_id, expires_at),
        )
    result: dict[str, Any] = {
        "id": proposal_id,
        "status": "pending",
        "payload": payload,
        "expires_at": expires_at.isoformat(),
        "replaces": replaces,
    }
    if replaces and replaces["status"] == "confirmed":
        result["net_days"] = _days(payload) - _days(replaces["payload"])
    return result


def list_my_proposals(ctx: RunContext, limit: int = 50) -> list[dict]:
    """The caller's own leave proposals, newest first (RLS also enforces tenant/user scope)."""
    with tx_with_context(ctx) as conn:
        rows = fetch_all(
            conn,
            """
            SELECT id, status, payload, created_at, confirmed_at
            FROM proposals
            WHERE company_id=%s AND owner_user_id=%s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (ctx.company_id, ctx.user_id, limit),
        )
    return [
        {
            "id": str(r["id"]),
            "status": r["status"],
            "payload": r["payload"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "confirmed_at": r["confirmed_at"].isoformat() if r["confirmed_at"] else None,
        }
        for r in rows
    ]


PROPOSAL_NOT_FOUND = "proposal not found"


def reject_proposal(ctx: RunContext, proposal_id: str) -> bool:
    with tx_with_context(ctx) as conn:
        row = conn.execute(
            "UPDATE proposals SET status='rejected' "
            "WHERE id=%s AND company_id=%s AND owner_user_id=%s AND status='pending' RETURNING id",
            (proposal_id, ctx.company_id, ctx.user_id),
        ).fetchone()
        return row is not None


def confirm_proposal(ctx: RunContext, proposal_id: str) -> tuple[bool, str]:
    """The only place leave is actually written. One transaction, rows locked, safe to call twice."""
    with tx_with_context(ctx) as conn:
        row = conn.execute("SELECT * FROM proposals WHERE id=%s FOR UPDATE", (proposal_id,)).fetchone()
        # Missing and not-yours look the same to the caller.
        if not row or row["company_id"] != ctx.company_id or row["owner_user_id"] != ctx.user_id:
            return False, PROPOSAL_NOT_FOUND
        if row["status"] == "confirmed":
            return True, "already confirmed"
        if row["status"] != "pending":
            return False, f"proposal status is {row['status']}"
        if row["expires_at"] < datetime.now(timezone.utc):
            conn.execute("UPDATE proposals SET status='expired' WHERE id=%s", (proposal_id,))
            return False, "proposal expired"

        days = _days(row["payload"])
        # If this changes a booking that is already confirmed, give those days back first.
        refund, amended_id = 0, None
        if row["supersedes_id"]:
            old = conn.execute(
                "SELECT id, status, payload FROM proposals WHERE id=%s FOR UPDATE", (row["supersedes_id"],)
            ).fetchone()
            if old and old["status"] == "amended":
                return False, "the booking this request changes was already changed; please ask again"
            if old and old["status"] == "confirmed":
                refund, amended_id = _days(old["payload"]), old["id"]
        net = days - refund

        bal_row = conn.execute(
            "SELECT days FROM leave_balances WHERE company_id=%s AND user_id=%s FOR UPDATE",
            (ctx.company_id, ctx.user_id),
        ).fetchone()
        if int(bal_row["days"] if bal_row else 0) < net:
            return False, "insufficient balance"

        conn.execute(
            "UPDATE leave_balances SET days=days-%s WHERE company_id=%s AND user_id=%s",
            (net, ctx.company_id, ctx.user_id),
        )
        if amended_id:
            conn.execute("UPDATE proposals SET status='amended' WHERE id=%s", (amended_id,))
        conn.execute("UPDATE proposals SET status='confirmed', confirmed_at=now() WHERE id=%s", (proposal_id,))
        details = {"proposal_id": proposal_id, "days": days, "net_days": net}
        if amended_id:
            details.update({"amends": str(amended_id), "refunded_days": refund})
        conn.execute(
            "INSERT INTO audit_log(company_id,user_id,action,details) VALUES (%s,%s,'proposal.confirmed',%s::jsonb)",
            (ctx.company_id, ctx.user_id, json.dumps(details)),
        )
        if amended_id:
            return True, f"confirmed; replaced the earlier booking, net change {net} day(s)"
        return True, "confirmed"
