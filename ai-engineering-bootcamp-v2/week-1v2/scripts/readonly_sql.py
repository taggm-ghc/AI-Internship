"""Run one read-only SQL statement against the project database (one Postgres instance; production).

The transaction is READ ONLY, so Postgres itself rejects any write (INSERT/UPDATE/DELETE/DDL, nextval).
A statement timeout bounds runtime; output is capped by --limit. Uses the project's own db module, so
the account and URL selection are the same as the app's (EXTERNAL_DB_URL locally).

Usage (repo root):
  ai-engineering-bootcamp-v2/week-1v2/.venv/bin/python ai-engineering-bootcamp-v2/week-1v2/scripts/readonly_sql.py "select ..." [--limit 200]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sql")
    ap.add_argument("--limit", type=int, default=200, help="max rows printed")
    ap.add_argument("--timeout-ms", type=int, default=15000)
    args = ap.parse_args(argv)
    from sqlalchemy import text
    from db import get_session
    with get_session() as s:
        s.execute(text("SET TRANSACTION READ ONLY"))
        s.execute(text(f"SET LOCAL statement_timeout = {int(args.timeout_ms)}"))
        result = s.execute(text(args.sql))
        print("\t".join(result.keys()))
        for i, row in enumerate(result):
            if i >= args.limit:
                print(f"... truncated at {args.limit} rows")
                break
            print("\t".join("" if c is None else str(c) for c in row))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
