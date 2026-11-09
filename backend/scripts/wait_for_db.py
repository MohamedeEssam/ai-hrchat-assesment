"""Wait for Postgres, then create the target database if it does not exist (e.g. ``hrchat_test``)."""
import time

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from db_urls import ADMIN_DATABASE_URL


def main() -> None:
    target_db = conninfo_to_dict(ADMIN_DATABASE_URL)["dbname"]
    server_url = make_conninfo(ADMIN_DATABASE_URL, dbname="postgres")

    for _ in range(120):
        try:
            with psycopg.connect(server_url, autocommit=True) as conn:
                exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_db,)).fetchone()
                if not exists:
                    conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_db)))
                    print(f"created database {target_db}")
                return
        except psycopg.OperationalError:
            time.sleep(1)
    raise RuntimeError("database did not become ready")


if __name__ == "__main__":
    main()
