import os

from app.core.auth import RunContext
from app.data.search import search_policy


def test_acl_maya_cannot_retrieve_ceo_doc() -> None:
    maya = RunContext(user_id="maya", company_id="summit", role="employee")
    results = search_policy(maya, "CEO salary band")

    assert all(row["title"] != "CEO salary band" for row in results)
    visible_text = "\n".join(row["text"] for row in results)
    assert "confidential" not in visible_text.lower()


def test_acl_leila_can_retrieve_ceo_doc() -> None:
    leila = RunContext(user_id="leila", company_id="summit", role="hr")
    results = search_policy(leila, "CEO salary band")

    assert any(row["title"] == "CEO salary band" for row in results)


def test_correct_policy_by_company() -> None:
    maya = RunContext(user_id="maya", company_id="summit", role="employee")
    ravi = RunContext(user_id="ravi", company_id="pebble", role="employee")

    summit = search_policy(maya, "lateness policy")
    pebble = search_policy(ravi, "lateness policy")

    summit_text = "\n".join(item["text"] for item in summit).lower()
    pebble_text = "\n".join(item["text"] for item in pebble).lower()

    assert "pay cut" in summit_text
    assert "warning only" in pebble_text


def test_db_policy_blocks_cross_tenant_reads_and_writes() -> None:
    import psycopg
    from pgvector.psycopg import register_vector

    conn = psycopg.connect(os.getenv("DATABASE_URL", "postgresql://app_user:app_pass@db:5432/hrchat"))
    register_vector(conn)

    try:
        with conn:
            with conn.transaction():
                conn.execute("SELECT set_config('app.company_id', 'summit', true)")
                conn.execute("SELECT set_config('app.user_id', 'maya', true)")
                conn.execute("SELECT set_config('app.role', 'employee', true)")

                rows = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
                assert rows == 2

                titles = conn.execute("SELECT title FROM documents ORDER BY title").fetchall()
                assert [t[0] for t in titles] == ["Late policy", "Poisoned note"]

                chunk_rows = conn.execute("SELECT text FROM chunks ORDER BY text").fetchall()
                assert all("CEO salary band: confidential" not in row[0] for row in chunk_rows)

                banned = conn.execute("SELECT count(*) FROM documents WHERE company_id = 'pebble'").fetchone()[0]
                assert banned == 0

                try:
                    conn.execute(
                        "INSERT INTO leave_balances(company_id, user_id, days) VALUES ('pebble', 'maya', 99)"
                    )
                    raise AssertionError("cross-company leave insert should have failed")
                except psycopg.Error:
                    pass

                try:
                    conn.execute("UPDATE leave_balances SET days = 999 WHERE user_id <> 'maya'")
                    raise AssertionError("cross-user leave update should have failed")
                except psycopg.Error:
                    pass
    finally:
        conn.close()
