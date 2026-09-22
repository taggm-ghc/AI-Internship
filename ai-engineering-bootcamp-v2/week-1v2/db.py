import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# Same pattern as ai-eng-bootcamp.vera/vera/db.py: Render sets RENDER=true in
# every one of its own build/runtime environments
# (https://render.com/docs/environment-variables). Off-Render (local dev, CI),
# that var is absent, so this falls through to the external URL. Everything
# downstream just calls get_engine()/get_session() and doesn't know or care
# which URL backed it.


def _database_url() -> str:
    # INTERNAL when this process is inside Render's own network (RENDER=true);
    # EXTERNAL from anywhere else, including this machine — INTERNAL_DB_URL's
    # hostname doesn't resolve off-Render, so there's no fallback between them.
    var = "INTERNAL_DB_URL" if os.getenv("RENDER") == "true" else "EXTERNAL_DB_URL"
    url = os.getenv(var)
    if not url:
        raise RuntimeError(f"{var} is not set (RENDER={os.getenv('RENDER')!r})")
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
