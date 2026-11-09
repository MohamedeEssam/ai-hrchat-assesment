"""Leave: my balance and requests, and Confirm / Reject for proposals."""
from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import RunContext, get_run_context, parse_id
from app.data.leave import (
    PROPOSAL_NOT_FOUND,
    confirm_proposal,
    get_leave_balance,
    list_my_proposals,
    reject_proposal,
)

router = APIRouter(tags=["leave"])


@router.get("/me/leave")
def my_leave(ctx: RunContext = Depends(get_run_context)) -> dict:
    """Own leave balance and own proposals only (powers the Leave requests view)."""
    return {"balance": get_leave_balance(ctx), "proposals": list_my_proposals(ctx)}


@router.post("/proposals/{proposal_id}/confirm")
def confirm(proposal_id: str, ctx: RunContext = Depends(get_run_context)) -> dict:
    ok, msg = confirm_proposal(ctx, parse_id(proposal_id, "proposal"))
    if not ok:
        # 404: missing or someone else's (same answer, so ids can't be probed). 409: wrong state / balance.
        raise HTTPException(status_code=404 if msg == PROPOSAL_NOT_FOUND else 409, detail=msg)
    return {"ok": True, "message": msg}


@router.post("/proposals/{proposal_id}/reject")
def reject(proposal_id: str, ctx: RunContext = Depends(get_run_context)) -> dict:
    if not reject_proposal(ctx, parse_id(proposal_id, "proposal")):
        raise HTTPException(status_code=409, detail="cannot reject proposal (not found or no longer pending)")
    return {"ok": True}
