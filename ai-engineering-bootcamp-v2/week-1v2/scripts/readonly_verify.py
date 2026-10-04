"""Read-only verification queries against the project database (one Postgres instance; production).

Only the named, fixed, aggregate queries below can run. No free SQL, no row text, no writes: every
query runs in a READ ONLY transaction through the project's own db module (so the same account and
URL selection as the app). The query names and SQL live in config/readonly_verify_queries.json so
the constants are not hardcoded here.

Usage (from the repo root):
  ai-engineering-bootcamp-v2/week-1v2/.venv/bin/python ai-engineering-bootcamp-v2/week-1v2/scripts/readonly_verify.py NAME
  ... readonly_verify.py --list
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CONFIG = ROOT / "config" / "readonly_verify_queries.json"
SELECT_ONLY = re.compile(r"^\s*select\b", re.I)
FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|call|do)\b|;", re.I)


def load_queries() -> dict:
    queries = json.loads(CONFIG.read_text())["queries"]
    for name, sql in queries.items():
        if not SELECT_ONLY.match(sql) or FORBIDDEN.search(sql):
            raise SystemExit(f"refusing: query {name!r} in {CONFIG.name} is not a single plain SELECT")
    return queries


def main(argv: list[str]) -> int:
    queries = load_queries()
    if argv == ["--list"]:
        print("\n".join(sorted(queries)))
        return 0
    if len(argv) != 1 or argv[0] not in queries:
        print(f"usage: readonly_verify.py NAME | --list ; names: {', '.join(sorted(queries))}", file=sys.stderr)
        return 2
    from sqlalchemy import text
    from db import get_session
    with get_session() as s:
        s.execute(text("SET TRANSACTION READ ONLY"))
        for row in s.execute(text(queries[argv[0]])):
            print(tuple(str(c) for c in row))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
