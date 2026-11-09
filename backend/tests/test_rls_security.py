import os

import psycopg
import pytest


def _postgres_conn() -> psycopg.Connection:
    return psycopg.connect(os.getenv("ADMIN_DATABASE_URL", "postgresql://postgres:postgres@db:5432/hrchat"))


def _set_ctx(conn: psycopg.Connection, company: str, user_id: str, role: str) -> None:
    conn.execute("SELECT set_config('app.company_id', %s, true)", (company,))
    conn.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))
    conn.execute("SELECT set_config('app.role', %s, true)", (role,))


def test_roles_and_rls_flags() -> None:
    with _postgres_conn() as conn:
        roles = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname IN ('app_user', 'migrator')"
            )
        }
        assert roles["app_user"] == (False, False)
        assert roles["migrator"] == (False, False)

        rows = conn.execute(
            """
            SELECT relname, relrowsecurity, relforcerowsecurity
            FROM pg_class
            WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'
            ORDER BY relname
            """
        ).fetchall()
        assert rows
        assert all(bool(row[1]) and bool(row[2]) for row in rows)


def test_no_context_returns_zero(conn: psycopg.Connection) -> None:
    docs = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
    chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    leave_balances = conn.execute("SELECT count(*) FROM leave_balances").fetchone()[0]
    runs = conn.execute("SELECT count(*) FROM runs").fetchone()[0]

    assert docs == 0
    assert chunks == 0
    assert leave_balances == 0
    assert runs == 0


@pytest.mark.parametrize(
    "company,user_id,role,expected_titles,expected_chunk_count",
    [
        ("summit", "maya", "employee", ["Late policy", "Poisoned note"], 2),
        ("summit", "leila", "hr", ["CEO salary band", "Late policy", "Poisoned note"], 3),
        ("pebble", "ravi", "employee", ["Employee handbook", "Late policy"], 2),
    ],
)
def test_read_visibility_matrix(
    conn: psycopg.Connection,
    company: str,
    user_id: str,
    role: str,
    expected_titles: list[str],
    expected_chunk_count: int,
) -> None:
    with conn.transaction():
        _set_ctx(conn, company, user_id, role)

        titles = [row[0] for row in conn.execute("SELECT title FROM documents ORDER BY title")]
        assert titles == expected_titles

        chunk_count = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        assert chunk_count == expected_chunk_count

        if user_id == "maya":
            texts = [row[0] for row in conn.execute("SELECT text FROM chunks ORDER BY text")]
            assert all("CEO salary band: confidential" not in text for text in texts)


def test_write_attempts_are_blocked(conn: psycopg.Connection) -> None:
    with conn.transaction():
        _set_ctx(conn, "summit", "maya", "employee")

        conn.execute("SAVEPOINT sp_insert")
        with pytest.raises(psycopg.Error):
            conn.execute(
                "INSERT INTO run_inputs(run_id, company_id, seq, text) VALUES (gen_random_uuid(), 'pebble', 1, 'x')"
            )
        conn.execute("ROLLBACK TO SAVEPOINT sp_insert")

        update_result = conn.execute("UPDATE leave_balances SET days = 999 WHERE user_id <> 'maya'")
        assert update_result.rowcount == 0

        conn.execute("SAVEPOINT sp_company")
        with pytest.raises(psycopg.Error):
            conn.execute("UPDATE leave_balances SET company_id='pebble' WHERE user_id='maya'")
        conn.execute("ROLLBACK TO SAVEPOINT sp_company")


def test_context_is_local_to_transaction(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute("SELECT set_config('app.company_id', 'summit', true)")

    value = conn.execute("SELECT current_setting('app.company_id', true)").fetchone()[0]
    assert value in (None, "")


def test_no_privilege_escalation_and_no_security_definer(conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.Error):
        conn.execute("SET ROLE postgres")

    with _postgres_conn() as postgres_conn:
        rows = postgres_conn.execute("SELECT proname FROM pg_proc WHERE prosecdef").fetchall()
        assert rows == []
