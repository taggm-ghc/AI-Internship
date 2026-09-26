"""Standalone connectivity check — run locally before pushing.

    python scripts/test_db_connection.py

Uses db.get_engine(): DB_ACCOUNT/DB_PASSWORD + DB_HOST/DB_NAME (bare host if
RENDER=true, host + DB_HOST_EXTERNAL_SUFFIX otherwise), falling back to
EXTERNAL_DB_URL/INTERNAL_DB_URL when DB_ACCOUNT is unset. Opens a
connection, runs `select 1`, and reports pass/fail. Touches no tables.
"""
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
load_dotenv(BASE / ".env")

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
