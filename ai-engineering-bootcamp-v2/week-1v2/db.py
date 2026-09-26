import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import URL, create_engine

# p3m3 item #47: every database setting lives in .env.db-accounts (gitignored,
# mode 600), not .env, and every DB path goes through this module, so loading
# it here covers main.py, scripts and checks alike. Absent on Render, where the
# same names are service environment variables (which take precedence anyway).
DB_SETTINGS_FILE = Path(__file__).resolve().parent / ".env.db-accounts"
load_dotenv(DB_SETTINGS_FILE)
from sqlalchemy.orm import Session, sessionmaker

# Same pattern as ai-eng-bootcamp.vera/vera/db.py: Render sets RENDER=true in
# every one of its own build/runtime environments
# (https://render.com/docs/environment-variables). Off-Render (local dev, CI),
# that var is absent, so this falls through to the external URL. Everything
# downstream just calls get_engine()/get_session() and doesn't know or care
# which URL backed it.


def _host() -> str:
    # Render's internal hostname is the bare host (dpg-<id>-a); the external
    # one is that host plus a regional suffix (.oregon-postgres.render.com).
    host = os.environ["DB_HOST"]
    return host if os.getenv("RENDER") == "true" else host + os.getenv("DB_HOST_EXTERNAL_SUFFIX", "")


def build_url(account: str, password: str) -> URL:
    """postgresql://account:password@host[.suffix]/db from DB_HOST,
    DB_HOST_EXTERNAL_SUFFIX and DB_NAME. URL.create escapes every part."""
    missing = [v for v in ("DB_HOST", "DB_NAME") if not os.getenv(v)]
    if missing:
        raise RuntimeError(f"{', '.join(missing)} not set")
    return URL.create("postgresql", username=account, password=password, host=_host(), database=os.environ["DB_NAME"])


def _database_url() -> str | URL:
    # p3m3 item #47: DB_ACCOUNT/DB_PASSWORD (a least-privilege _rw account)
    # plus the host/database parts. Without DB_ACCOUNT, fall back to the full
    # URL. INTERNAL when this process is inside Render's own network
    # (RENDER=true); EXTERNAL from anywhere else, including this machine —
    # INTERNAL_DB_URL's hostname doesn't resolve off-Render, so there's no
    # fallback between them.
    if os.getenv("DB_ACCOUNT"):
        return build_url(os.environ["DB_ACCOUNT"], os.getenv("DB_PASSWORD", ""))
    var = "INTERNAL_DB_URL" if os.getenv("RENDER") == "true" else "EXTERNAL_DB_URL"
    url = os.getenv(var)
    if not url:
        raise RuntimeError(f"DB_ACCOUNT and {var} are both unset (RENDER={os.getenv('RENDER')!r})")
    return url


_engine = None
SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine, SessionLocal
    if _engine is None:
        _engine = create_engine(_database_url(), pool_pre_ping=True, hide_parameters=True, connect_args={"connect_timeout": 10})
        SessionLocal = sessionmaker(bind=_engine)
    return _engine


def get_session() -> Session:
    get_engine()
    assert SessionLocal is not None
    return SessionLocal()


# Admin connection (p3m3 item #47): schema changes only (install_schema,
# create_vector_indexes, the one-off migration). The app itself never uses it.
# No account name is hardcoded: DB_ADMIN_ROLE is tried first and
# DB_ADMIN_ROLE_RETRY if that login is rejected, both with DB_ADMIN_PASSWORD
# and the same host/database.
# Best practice: the admin account is named for its privilege (*_dbadmin).
# Current state (unintended, accepted): the admin is Render's original
# `<db>_user` account, which holds CREATEROLE/CREATEDB and owns every object.
# Render can't rename roles, and a non-superuser can't rename itself, so the
# retry (meant for a rename to *_dbadmin) is dormant.
_AUTH_FAILURES = ("password authentication failed", "does not exist")
_admin_engine = None


def _admin_roles() -> list[str]:
    roles = [os.getenv("DB_ADMIN_ROLE"), os.getenv("DB_ADMIN_ROLE_RETRY")]
    return list(dict.fromkeys(r for r in roles if r))


def get_admin_engine():
    """Engine for the admin account; tries DB_ADMIN_ROLE, then DB_ADMIN_ROLE_RETRY.
    Retries only a rejected login, never a network or other failure."""
    global _admin_engine
    if _admin_engine is not None:
        return _admin_engine
    from sqlalchemy.exc import OperationalError

    roles = _admin_roles()
    if not roles or not os.getenv("DB_ADMIN_PASSWORD"):
        raise RuntimeError("DB_ADMIN_ROLE and DB_ADMIN_PASSWORD must be set for admin work (schema changes)")
    for i, role in enumerate(roles):
        engine = create_engine(build_url(role, os.environ["DB_ADMIN_PASSWORD"]), pool_pre_ping=True, hide_parameters=True, connect_args={"connect_timeout": 10})
        try:
            with engine.connect():
                pass
        except OperationalError as exc:
            engine.dispose()
            if i + 1 < len(roles) and any(s in str(exc.orig) for s in _AUTH_FAILURES):
                continue
            raise
        _admin_engine = engine
        return engine
    raise RuntimeError("no admin role configured")
