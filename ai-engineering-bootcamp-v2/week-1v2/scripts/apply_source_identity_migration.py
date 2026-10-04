"""Apply migration 006 (source identity tables, item #65).

Dry-run by default: prints the SQL, the grants and a read-only plan (which tables exist). --apply runs the
migration and the grants in ONE transaction as the admin account (db.get_admin_engine()). No roles are created.
"""
import argparse
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv

load_dotenv(BASE / '.env')
from sqlalchemy import text

MIGRATION = BASE / 'migrations' / '006_source_identity.sql'
SCHEMA = 'internship'
# Least privilege: the app role gets INSERT and SELECT only (no UPDATE/DELETE); the read-only group gets SELECT.
# source_identifier_types is read-only for the app (changed only by migration).
RW_GRANTS = {
    'identifiers': 'INSERT, SELECT',
    'sources': 'INSERT, SELECT',
    'source_identifiers': 'INSERT, SELECT',
    'document_sources': 'INSERT, SELECT',
}
SELECT_ONLY = ['source_identifier_types']
SEQUENCES = ['identifiers_id_seq', 'sources_id_seq']


def q(ident):
    return '"' + ident.replace('"', '""') + '"'


def grant_statements(rw_group, ro_group):
    rw, ro = q(rw_group), q(ro_group)
    sql = []
    for table in list(RW_GRANTS) + SELECT_ONLY:
        sql.append(f'GRANT SELECT ON {SCHEMA}.{table} TO {ro}')
    for table, privs in RW_GRANTS.items():
        sql.append(f'GRANT {privs} ON {SCHEMA}.{table} TO {rw}')
    for seq in SEQUENCES:
        sql.append(f'GRANT USAGE ON SEQUENCE {SCHEMA}.{seq} TO {rw}')
    return sql


def group_names():
    missing = [n for n in ('DB_RW_GROUP', 'DB_RO_GROUP') if not os.getenv(n)]
    if missing:
        raise SystemExit(f'{", ".join(missing)} not set in .env')
    return os.environ['DB_RW_GROUP'], os.environ['DB_RO_GROUP']


def plan(engine):
    tables = list(RW_GRANTS) + SELECT_ONLY
    with engine.connect() as conn:
        found = {r[0] for r in conn.execute(text(
            'SELECT table_name FROM information_schema.tables WHERE table_schema = :s'), {'s': SCHEMA})}
    for t in tables:
        print(f'  {SCHEMA}.{t}: {"exists (migration is a no-op)" if t in found else "will be created"}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='run the migration and grants (default: dry run)')
    args = parser.parse_args()
    sql = MIGRATION.read_text()
    rw, ro = group_names()
    grants = grant_statements(rw, ro)
    print(f'-- {MIGRATION.name}\n{sql}\n-- grants (group names from .env)')
    print('\n'.join(g + ';' for g in grants))
    try:
        from db import get_admin_engine
        engine = get_admin_engine()
        if not args.apply:
            print('\nDry run: read-only plan (no changes). Re-run with --apply to execute.')
            plan(engine)
            return
        with engine.begin() as conn:
            conn.exec_driver_sql(sql)
            for g in grants:
                conn.exec_driver_sql(g)
        print('Applied migration 006 and grants in one transaction.')
    except SystemExit:
        raise
    except Exception as exc:
        print(f'Failed: {type(exc).__name__} (connection details withheld); transaction rolled back', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
