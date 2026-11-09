"""Jobs: start one, add a note while it works, and stream its events (SSE)."""
import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from app.agent.orchestrator import OrchestratorManager
from app.api.deps import RunContext, get_manager, get_run_context, parse_id
from app.data.runs import create_run, get_run, load_history

router = APIRouter(prefix="/runs", tags=["runs"])


class RunBody(BaseModel):
    message: str
    # Earlier runs of the same chat session; only the caller's own runs are loaded.
    context_run_ids: list[str] = []


class NoteBody(BaseModel):
    text: str


@router.post("")
async def start_run(
    body: RunBody,
    ctx: RunContext = Depends(get_run_context),
    mgr: OrchestratorManager = Depends(get_manager),
) -> dict:
    try:
        context_ids = [str(uuid.UUID(r)) for r in body.context_run_ids[-20:]]
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid context_run_ids")
    history = load_history(ctx, context_ids)
    run_id = create_run(ctx, body.message)
    await mgr.start(run_id, ctx, history)
    return {"run_id": run_id}


@router.get("/{run_id}")
def read_run(run_id: str, ctx: RunContext = Depends(get_run_context)) -> dict:
    row = get_run(ctx, parse_id(run_id, "run"))
    if not row:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run": row}


@router.post("/{run_id}/notes")
async def post_note(
    run_id: str,
    body: NoteBody,
    ctx: RunContext = Depends(get_run_context),
    mgr: OrchestratorManager = Depends(get_manager),
) -> dict:
    """Add an instruction to a job that is still working. 409 if it already finished (start a follow-up run)."""
    run_id = parse_id(run_id, "run")
    if not get_run(ctx, run_id):
        raise HTTPException(status_code=404, detail="run not found")
    seq = await mgr.add_note(ctx, run_id, body.text)
    if seq is None:
        raise HTTPException(status_code=409, detail="run already finished; send a new message instead")
    return {"ok": True, "seq": seq}


@router.get("/{run_id}/events")
async def stream_events(
    run_id: str,
    ctx: RunContext = Depends(get_run_context),
    mgr: OrchestratorManager = Depends(get_manager),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    once: bool = False,
):
    """Server-Sent Events. Every event is stored first, so Last-Event-ID resumes without gaps."""
    run_id = parse_id(run_id, "run")
    if not get_run(ctx, run_id):
        raise HTTPException(status_code=404, detail="run not found")
    try:
        start = max(int(last_event_id or "0"), 0)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid Last-Event-ID")

    async def event_stream():
        seq = start
        empty_rounds = 0
        while True:
            events = await mgr.wait_for_events(ctx, run_id, seq)
            if not events:
                empty_rounds += 1
                if once and empty_rounds > 5:
                    break
                await asyncio.sleep(0.2)
                continue
            for event in events:
                seq = int(event["seq"])
                yield f"id: {seq}\nevent: {event['type']}\ndata: {json.dumps(event['payload'])}\n\n"
            if once:
                break

    return StreamingResponse(event_stream(), media_type="text/event-stream")
