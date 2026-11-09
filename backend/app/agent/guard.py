"""Deterministic guard for questions about another company.

The database already makes other companies' data unreachable (RLS). This guard fixes a different
problem: a small model may still *talk* about another company, e.g. present the user's own policy as
the other company's, or invent one. A request that names another company is therefore refused here,
in code, before any model call.
"""
from __future__ import annotations

import re

from app.core.auth import TOKENS


def known_companies() -> set[str]:
    # Company ids of the configured tenants. The companies table is behind RLS (a user only sees
    # their own row), so the names come from configuration, not from the database.
    return {ctx.company_id for ctx in TOKENS.values()}


def other_company_mentioned(own_company: str, texts: list[str]) -> str | None:
    """The first other company named in the user's text, if any (whole-word, case-insensitive)."""
    blob = " ".join(texts).lower()
    for company in sorted(known_companies() - {own_company}):
        if re.search(rf"\b{re.escape(company.lower())}\b", blob):
            return company
    return None


def refusal_text(own_company: str, other_company: str) -> str:
    return (
        f"I can only help with {own_company.title()} information, so I can't answer questions about "
        f"{other_company.title()}."
    )
