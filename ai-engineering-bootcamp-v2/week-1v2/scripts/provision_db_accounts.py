"""Least-privilege database accounts (p3m3 item #47, OWASP ASI03).

The admin account (db.get_admin_engine: DB_ADMIN_ROLE, retried as
DB_ADMIN_ROLE_RETRY) creates NOLOGIN groups and LOGIN user accounts; the app,
local agent runs and (optionally) backups then connect as those, never as the admin. Every
account name comes from .env; none is hardcoded here.

Modes (default --dry-run executes nothing):
  --dry-run             print the SQL, passwords shown as <redacted>
  --rehash-admin-scram  re-save the admin's CURRENT password as SCRAM (same
                        password, new hash) so a rename can't clear it; md5
                        hashes are salted with the role name
  --apply               create groups/accounts/grants in one transaction;
                        new passwords are upserted into .env.db-accounts
                        (DB_ACCOUNT_PASSWORD_<account>; never printed)
  --verify              privilege matrix + a login as each new account;
                        no data writes
"""
import argparse
import os
import secrets
import stat
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv

load_dotenv(BASE / ".env")
from sqlalchemy import create_engine, text

from db import DB_SETTINGS_FILE, build_url, get_admin_engine

SCHEMA = "internship"
# Write grants derived from the app's own statements (operational_store.py,
# providers.py, rag_service.py). events/provider_observations are append-only.
RW_GRANTS = {
    "events": "INSERT",
    "provider_observations": "INSERT",
    "artifacts": "INSERT, UPDATE",
    "collections": "INSERT, UPDATE",
    "documents": "INSERT, UPDATE",
    "document_versions": "INSERT, UPDATE",
    "vectors": "INSERT, UPDATE, DELETE",
}
READ_ONLY_OBJECTS = ["schema_version", "document_provenance"]


def env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        sys.exit(f"{name} is not set in .env")
    return value


def names() -> dict:
    logins = {  # account -> group
        env("DB_API_RW_USER"): env("DB_RW_GROUP"),
        env("DB_LOCAL_AGENT_RW_USER"): env("DB_RW_GROUP"),
    }
    # Best practice: backups run as their own read-only account (pg_dump needs
    # only SELECT), so a leaked backup credential can't change data. Optional:
    # created only when DB_BACKUP_RO_USER is set.
    if os.getenv("DB_BACKUP_RO_USER"):
        logins[os.environ["DB_BACKUP_RO_USER"]] = env("DB_RO_GROUP")
    return {"ro_group": env("DB_RO_GROUP"), "rw_group": env("DB_RW_GROUP"), "logins": logins}


def q(ident: str) -> str:
    """Quote an identifier taken from .env."""
    return '"' + ident.replace('"', '""') + '"'


def statements(n: dict, database: str) -> list[tuple[str, dict]]:
    ro, rw = q(n["ro_group"]), q(n["rw_group"])
    sql = [
        ("SET LOCAL password_encryption = 'scram-sha-256'", {}),
        (f"CREATE ROLE {ro} NOLOGIN", {}),
        (f"GRANT CONNECT ON DATABASE {q(database)} TO {ro}", {}),
        (f"GRANT USAGE ON SCHEMA {SCHEMA} TO {ro}", {}),
        (f"GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO {ro}", {}),
        (f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} GRANT SELECT ON TABLES TO {ro}", {}),
        (f"CREATE ROLE {rw} NOLOGIN IN ROLE {ro}", {}),
    ]
    sql += [(f"GRANT {privs} ON {SCHEMA}.{table} TO {rw}", {}) for table, privs in RW_GRANTS.items()]
    for account, group in n["logins"].items():
        # CREATE ROLE can't take a bind parameter; the password is a
        # token_urlsafe string ([A-Za-z0-9_-]), so literal quoting is safe.
        sql.append((f"CREATE ROLE {q(account)} LOGIN IN ROLE {q(group)} PASSWORD :pw", {"pw_for": account}))
    return sql


def render(sql: str, pw: str | None) -> str:
    return sql.replace(":pw", f"'{pw}'" if pw else "'<redacted>'")


def run_secret(conn, sql: str, redacted: str) -> None:
    """Execute a statement that embeds a password; on failure, report the
    redacted statement and the server's message, never the statement itself
    (SQLAlchemy errors otherwise echo it)."""
    try:
        conn.exec_driver_sql(sql)
    except Exception as exc:
        orig = getattr(exc, "orig", None)
        message = getattr(getattr(orig, "diag", None), "message_primary", None) or type(exc).__name__
        raise SystemExit(f"failed: {redacted}\n  server said: {message}") from None


def credentials_path() -> Path:
    return Path(os.getenv("DB_ACCOUNTS_OUT", DB_SETTINGS_FILE))


def password_key(account: str) -> str:
    return f"DB_ACCOUNT_PASSWORD_{account}"


def read_passwords() -> dict:
    path = credentials_path()
    lines = path.read_text().splitlines() if path.exists() else []
    return {k: v for k, _, v in (l.partition("=") for l in lines if l and not l.startswith("#"))}


def write_credentials(passwords: dict, n: dict) -> Path:
    """Upsert one DB_ACCOUNT_PASSWORD_<account> line per new account into the
    DB settings file, leaving every other line as it is. A consumer sets
    DB_ACCOUNT=<account> and DB_PASSWORD=<that value>; host and database come
    from DB_HOST/DB_HOST_EXTERNAL_SUFFIX/DB_NAME (db.build_url)."""
    out = credentials_path()
    lines = out.read_text().splitlines() if out.exists() else []
    for account, pw in passwords.items():
        key = password_key(account)
        line = f"{key}={pw}"
        idx = next((i for i, l in enumerate(lines) if l.startswith(key + "=")), None)
        if idx is None:
            lines.append(line)
        else:
            lines[idx] = line
    out.write_text("\n".join(lines) + "\n")
    out.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return out


def verify(n: dict) -> int:
    admin_engine = get_admin_engine()
    fails = 0

    def check(label, ok):
        nonlocal fails
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'}: {label}", flush=True)

    all_tables = list(RW_GRANTS) + READ_ONLY_OBJECTS
    with admin_engine.connect() as c:
        for account, group in n["logins"].items():
            writer = group == n["rw_group"]
            for table in all_tables:
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                    want = priv == "SELECT" or (writer and priv in RW_GRANTS.get(table, ""))
                    have = c.execute(text("SELECT has_table_privilege(:r, :t, :p)"),
                                     {"r": account, "t": f"{SCHEMA}.{table}", "p": priv}).scalar()
                    if have != want:
                        check(f"{account} {priv} on {table}: expected {want}, got {have}", False)
            create = c.execute(text("SELECT has_schema_privilege(:r, :s, 'CREATE')"), {"r": account, "s": SCHEMA}).scalar()
            attrs = c.execute(text("SELECT rolcreaterole OR rolcreatedb OR rolsuper FROM pg_roles WHERE rolname = :r"), {"r": account}).scalar()
            check(f"{account}: privilege matrix as designed, no CREATE on schema, no admin attributes", not create and attrs is False)
    creds = read_passwords()
    for account in [a for a in n["logins"] if password_key(a) in creds]:
        engine = create_engine(build_url(account, creds[password_key(account)]), connect_args={"connect_timeout": 10})
        with engine.connect() as c:
            c.execute(text("SET TRANSACTION READ ONLY"))
            who = c.execute(text("SELECT current_user")).scalar()
            count = c.execute(text(f"SELECT count(*) FROM {SCHEMA}.documents")).scalar()
        engine.dispose()
        check(f"login as {who} works; reads {count} documents", who == account and count > 0)
    print(f"\n{'all passed' if not fails else f'{fails} failed'}")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--rehash-admin-scram", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    n = names()

    if args.verify:
        return verify(n)
    if args.rehash_admin_scram:
        engine = get_admin_engine()
        password = env("DB_ADMIN_PASSWORD")
        with engine.begin() as c:
            c.execute(text("SET LOCAL password_encryption = 'scram-sha-256'"))
            run_secret(c, f"ALTER ROLE CURRENT_USER PASSWORD '{password.replace(chr(39), chr(39) * 2)}'",
                       "ALTER ROLE CURRENT_USER PASSWORD '<redacted>'")
        engine.dispose()
        check = create_engine(build_url(engine.url.username, password), connect_args={"connect_timeout": 10})
        with check.connect() as c:
            print(f"re-hashed as SCRAM; fresh login as {c.execute(text('SELECT current_user')).scalar()} with the same password works")
        return 0

    database = env("DB_NAME")
    plan = statements(n, database)
    if not args.apply:
        print("-- DRY RUN: nothing executed\nBEGIN;")
        for sql, _ in plan:
            print(render(sql, None) + ";")
        print("COMMIT;")
        return 0

    passwords = {account: secrets.token_urlsafe(32) for account in n["logins"]}
    out = write_credentials(passwords, n)  # before any SQL, so no password is ever lost
    with get_admin_engine().begin() as c:
        for sql, meta in plan:
            run_secret(c, render(sql, passwords.get(meta.get("pw_for"))), render(sql, None))
    print(f"applied; passwords for the new accounts written to {out} (mode 600)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
