"""Audit trail: confirms, rejects and blocked scope-override attempts."""
from __future__ import annotations

import json
from typing import Any

from app.core.auth import RunContext
from app.core.db import fetch_all, tx_with_context


def audit(ctx: RunContext, action: str, details: dict[str, Any]) -> None:
    with tx_with_context(ctx) as conn:
        conn.execute(
            "INSERT INTO audit_log(company_id,user_id,action,details) VALUES (%s,%s,%s,%s::jsonb)",
            (ctx.company_id, ctx.user_id, action, json.dumps(details)),
        )


def load_audit(ctx: RunContext) -> list[dict]:
    with tx_with_context(ctx) as conn:
        return fetch_all(conn, "SELECT action, details FROM audit_log ORDER BY id")
