from contextlib import contextmanager
from typing import Iterator, Any

import psycopg
from psycopg.rows import dict_row

from app.core.auth import RunContext
from app.core.config import settings


def get_conn() -> psycopg.Connection:
    return psycopg.connect(settings.database_url, row_factory=dict_row)


@contextmanager
def tx_with_context(ctx: RunContext) -> Iterator[psycopg.Connection]:
    with get_conn() as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('app.company_id', %s, true)", (ctx.company_id,))
            conn.execute("SELECT set_config('app.user_id', %s, true)", (ctx.user_id,))
            conn.execute("SELECT set_config('app.role', %s, true)", (ctx.role,))
            yield conn


def fetch_all(conn: psycopg.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict]:
    return list(conn.execute(sql, params).fetchall())


def fetch_one(conn: psycopg.Connection, sql: str, params: tuple[Any, ...] = ()) -> dict | None:
    return conn.execute(sql, params).fetchone()
