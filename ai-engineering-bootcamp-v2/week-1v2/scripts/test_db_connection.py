"""Standalone connectivity check — run locally before pushing.

    python scripts/test_db_connection.py

Reads EXTERNAL_DB_URL from .env (or INTERNAL_DB_URL if RENDER=true), opens a
connection, runs `select 1`, and reports pass/fail. Touches no tables.
"""
import sys

from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()

from db import get_engine  # noqa: E402  (after load_dotenv, matches main.py's ordering)


def main() -> int:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"OK — connected via {engine.url.render_as_string(hide_password=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
