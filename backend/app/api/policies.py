"""GET /policies/search: the same search the bot uses, run as the caller."""
from fastapi import APIRouter, Depends

from app.api.deps import RunContext, get_run_context
from app.data.search import search_policy

router = APIRouter(tags=["policies"])


@router.get("/policies/search")
def policies_search(q: str, ctx: RunContext = Depends(get_run_context)) -> dict:
    return {"items": search_policy(ctx, q)}
