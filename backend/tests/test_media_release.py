from __future__ import annotations

import logging
import os
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.auth import AuthUser, get_current_user
from app.db.base import Base
from app.db.models import Client, UserRole
from app.db.session import get_db
from app.main import app
from app.media_store import (
    delete_unreferenced_media,
    iter_media_backup_paths,
    note_media_for_release,
)


def _name(letter: str) -> str:
    return (letter * 32) + ".jpg"


@pytest.fixture()
def media_root(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setenv("LB_MEDIA_ROOT", str(root))
    return root


@pytest.fixture()
def memory_db():
    from app.db import models as _orm_models  # noqa: F401

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()


def test_replace_deletes_old_file_after_commit(media_root, memory_db) -> None:
    old = _name("a")
    new = _name("b")
    (media_root / old).write_bytes(b"old")
    (media_root / new).write_bytes(b"new")
    client = Client(name="Аня", photo_1=f"/media/{old}")
    memory_db.add(client)
    memory_db.commit()

    note_media_for_release(memory_db, client.photo_1)
    client.photo_1 = f"/media/{new}"
    memory_db.commit()

    assert not (media_root / old).exists()
    assert (media_root / new).exists()


def test_shared_file_stays_until_last_reference_is_gone(media_root, memory_db) -> None:
    name = _name("c")
    path = media_root / name
    path.write_bytes(b"shared")
    url = f"/media/{name}"
    first = Client(name="Одна", photo_1=url)
    second = Client(name="Другая", photo_1=url)
    memory_db.add_all([first, second])
    memory_db.commit()

    note_media_for_release(memory_db, first.photo_1)
    first.photo_1 = None
    memory_db.commit()
    assert path.exists()

    note_media_for_release(memory_db, second.photo_1)
    second.photo_1 = None
    memory_db.commit()
    assert not path.exists()


def test_rollback_keeps_the_file(media_root, memory_db) -> None:
    name = _name("d")
    path = media_root / name
    path.write_bytes(b"keep")
    client = Client(name="Вера", photo_1=f"/media/{name}")
    memory_db.add(client)
    memory_db.commit()

    note_media_for_release(memory_db, client.photo_1)
    client.photo_1 = None
    memory_db.rollback()
    memory_db.commit()
    assert path.exists()


def test_unlink_error_does_not_fail_commit(media_root, memory_db, monkeypatch, caplog) -> None:
    name = _name("e")
    path = media_root / name
    path.write_bytes(b"stuck")
    client = Client(name="Галя", photo_1=f"/media/{name}")
    memory_db.add(client)
    memory_db.commit()

    def boom(_rel: str) -> None:
        raise OSError("disk")

    monkeypatch.setattr("app.media_store._unlink_stored_rel", boom)
    note_media_for_release(memory_db, client.photo_1)
    client.photo_1 = None
    with caplog.at_level(logging.WARNING):
        memory_db.commit()
    assert path.exists()
    assert "Не удалось удалить медиафайл" in caplog.text


def test_cleanup_skips_fresh_and_referenced_files(media_root, memory_db) -> None:
    fresh = media_root / _name("a")
    orphan = media_root / _name("b")
    referenced = media_root / _name("c")
    fresh.write_bytes(b"fresh")
    orphan.write_bytes(b"orphan-bytes")
    referenced.write_bytes(b"ref")
    past = time.time() - 7200
    os.utime(orphan, (past, past))
    os.utime(referenced, (past, past))
    memory_db.add(Client(name="Дина", photo_1=f"/media/{referenced.name}"))
    memory_db.commit()

    names_before = {path.name for path in iter_media_backup_paths(media_root)}
    assert {fresh.name, orphan.name, referenced.name} <= names_before

    deleted, freed = delete_unreferenced_media(memory_db)
    assert deleted == 1
    assert freed == len(b"orphan-bytes")
    assert not orphan.exists()
    assert fresh.exists()
    assert referenced.exists()
    assert orphan.name not in {path.name for path in iter_media_backup_paths(media_root)}
    assert fresh.name in {path.name for path in iter_media_backup_paths(media_root)}


def test_orphan_cleanup_route(media_root, memory_db) -> None:
    orphan = media_root / _name("d")
    orphan.write_bytes(b"old")
    past = time.time() - 7200
    os.utime(orphan, (past, past))

    def _db():
        yield memory_db

    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        id=1,
        username="tech",
        display_name="Tech",
        role=UserRole.TECHSPEC,
        roles=(UserRole.TECHSPEC,),
    )
    app.dependency_overrides[get_db] = _db
    try:
        res = TestClient(app).post("/techspec/media/orphans", follow_redirects=False)
    finally:
        app.dependency_overrides.clear()
    assert res.status_code == 303
    assert res.headers["location"] == "/?orphans_deleted=1"
    assert not orphan.exists()
