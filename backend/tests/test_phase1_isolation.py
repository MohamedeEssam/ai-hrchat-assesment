from app.core.auth import RunContext
from app.data.search import search_policy


def test_isolation_search_tool() -> None:
    summit_ctx = RunContext(user_id="maya", company_id="summit", role="employee")
    pebble_ctx = RunContext(user_id="ravi", company_id="pebble", role="employee")

    summit_items = search_policy(summit_ctx, "lateness policy")
    pebble_items = search_policy(pebble_ctx, "lateness policy")

    assert summit_items
    assert pebble_items
    assert all(row["title"] != "Employee handbook" for row in summit_items)
    assert all("pay cut" not in row["text"] for row in pebble_items)


def test_isolation_raw_sql_under_rls(conn) -> None:
    with conn.transaction():
        conn.execute("SELECT set_config('app.company_id', %s, true)", ("summit",))
        conn.execute("SELECT set_config('app.user_id', %s, true)", ("maya",))
        conn.execute("SELECT set_config('app.role', %s, true)", ("employee",))
        count = conn.execute("SELECT count(*) FROM documents WHERE company_id='pebble'").fetchone()[0]
        assert count == 0

    with conn.transaction():
        count = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
        assert count == 0
