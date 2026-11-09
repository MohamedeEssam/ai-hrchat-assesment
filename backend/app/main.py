"""FastAPI entry point: builds the app and wires the routers.

Layout:
    core/   settings, auth (token -> RunContext), db (transaction with RLS context)
    data/   all SQL: runs/events, leave + proposals, audit, policy search
    agent/  the job loop: orchestrator, planner/answer prompts, tools, LLM client
    api/    HTTP routes, one module per area
    static/ the chat page
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.agent.llm import OpenAICompatibleLLM
from app.agent.orchestrator import OrchestratorManager
from app.api import auth, leave, policies, runs

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="hrchat")
app.state.manager = OrchestratorManager(llm=OpenAICompatibleLLM())
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

for module in (auth, runs, leave, policies):
    app.include_router(module.router)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict:
    return {"ok": True}
