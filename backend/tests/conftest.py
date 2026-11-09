import os

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.agent.llm import ScriptedLLM
from app.agent.orchestrator import OrchestratorManager
from app.main import app


@pytest.fixture(scope="session")
def app_db_url() -> str:
    return os.getenv("DATABASE_URL", "postgresql://app_user:app_pass@db:5432/hrchat")


@pytest.fixture()
def conn(app_db_url: str):
    with psycopg.connect(app_db_url) as connection:
        yield connection


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def scripted_manager() -> tuple[ScriptedLLM, OrchestratorManager]:
    llm = ScriptedLLM()
    mgr = OrchestratorManager(llm=llm)
    app.state.manager = mgr
    return llm, mgr


def set_ctx(connection: psycopg.Connection, company: str | None, user_id: str | None, role: str | None) -> None:
    with connection.transaction():
        if company is not None:
            connection.execute("SELECT set_config('app.company_id', %s, true)", (company,))
        if user_id is not None:
            connection.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))
        if role is not None:
            connection.execute("SELECT set_config('app.role', %s, true)", (role,))
