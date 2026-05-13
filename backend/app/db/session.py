from __future__ import annotations

"""DB engine and request-scoped session helper (`get_db`)."""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.settings import get_settings


def _ensure_sqlite_parent_dir(database_url: str) -> None:
    """Создать родительскую папку для файла SQLite (например, `data/`), если её ещё нет."""
    url = make_url(database_url)
    if url.drivername != "sqlite":
        return
    db = url.database
    if not db or db == ":memory:":
        return
    path = Path(db)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.resolve().parent.mkdir(parents=True, exist_ok=True)


def _make_engine():
    settings = get_settings()
    connect_args: dict = {}
    engine_kw: dict = {"future": True}
    if settings.database_url.startswith("sqlite"):
        _ensure_sqlite_parent_dir(settings.database_url)
        connect_args = {"check_same_thread": False}
    else:
        # После простоя PostgreSQL / прокси часто закрывают TCP+SSL; без этого пул отдаёт «мёртвое»
        # соединение → OperationalError до первого refresh. pre_ping проверяет канал при checkout;
        # pool_recycle укладывается в типичные idle-timeout (например RDS ~300 с).
        engine_kw["pool_pre_ping"] = True
        engine_kw["pool_recycle"] = 280
    if connect_args:
        engine_kw["connect_args"] = connect_args
    return create_engine(settings.database_url, **engine_kw)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()

