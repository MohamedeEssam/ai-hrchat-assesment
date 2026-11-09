"""Apply the schema (idempotent: safe to run on every start)."""
import os
import subprocess

from db_urls import ADMIN_DATABASE_URL


def main() -> None:
    subprocess.run(
        ["psql", ADMIN_DATABASE_URL, "-v", "ON_ERROR_STOP=1", "-q", "-f", "sql/001_schema.sql"],
        check=True,
        # Hide "does not exist, skipping" notices from the idempotent DROP ... IF EXISTS statements.
        env={**os.environ, "PGOPTIONS": "-c client_min_messages=warning"},
    )


if __name__ == "__main__":
    main()
