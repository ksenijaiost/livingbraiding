from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.auth import AuthUser, get_current_user
from app.db.models import CareMemo, Client, StudioTemplateVariable, User, UserRole, VisitDraft
from app.db.session import get_db
from app.main import app
from app.media_store import (
    MAX_BYTES,
    MEDIA_MISSING_REPORT_NAME,
    MediaBackupEntry,
    build_media_manifest,
    filter_entries_missing_from_manifest,
    is_stored_media_backup_filename,
    iter_media_backup_entries,
    iter_media_backup_paths,
    media_refs_in_text,
    missing_referenced_media_files,
    parse_media_manifest,
    split_media_entries_into_parts,
)
from app.routes.techspec_media import _MAX_RESTORE_UNCOMPRESSED


def _techspec_user() -> AuthUser:
    return AuthUser(
        id=1,
        username="tech",
        display_name="Tech",
        role=UserRole.TECHSPEC,
        roles=(UserRole.TECHSPEC,),
    )


def _media_name(letter: str, ext: str) -> str:
    return (letter * 32) + ext


@pytest.fixture()
def media_root(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setenv("LB_MEDIA_ROOT", str(root))
    return root


def test_build_media_manifest_only_valid_names(media_root: Path) -> None:
    (media_root / _media_name("a", ".jpg")).write_bytes(b"x" * 100)
    (media_root / _media_name("b", ".png")).write_bytes(b"y" * 200)
    (media_root / "notes.txt").write_text("skip")

    manifest = build_media_manifest(media_root)
    assert manifest["version"] == 1
    assert manifest["file_count"] == 2
    assert manifest["total_bytes"] == 300
    names = {f["name"] for f in manifest["files"]}
    assert len(names) == 2
    assert "notes.txt" not in names


def test_filter_entries_missing_from_manifest(media_root: Path) -> None:
    old = media_root / _media_name("a", ".jpg")
    new = media_root / _media_name("b", ".jpg")
    old.write_bytes(b"old")
    new.write_bytes(b"new")

    entries = iter_media_backup_entries(media_root)
    manifest = parse_media_manifest({"files": [{"name": old.name, "size": 3, "mtime": 1}]})
    missing = filter_entries_missing_from_manifest(entries, manifest)

    assert len(missing) == 1
    assert missing[0].name == new.name


def test_filter_entries_missing_none_when_up_to_date(media_root: Path) -> None:
    p = media_root / _media_name("a", ".jpg")
    p.write_bytes(b"x")
    entries = iter_media_backup_entries(media_root)
    manifest = build_media_manifest(media_root)
    assert filter_entries_missing_from_manifest(entries, manifest) == []


def test_get_manifest_json(media_root: Path) -> None:
    (media_root / _media_name("a", ".jpg")).write_bytes(b"x")
    app.dependency_overrides[get_current_user] = _techspec_user
    try:
        client = TestClient(app)
        res = client.get("/techspec/media/manifest.json")
        assert res.status_code == 200
        data = res.json()
        assert data["file_count"] == 1
        assert data["files"][0]["name"].endswith(".jpg")
    finally:
        app.dependency_overrides.clear()


def test_post_backup_delta_zip(media_root: Path, memory_db) -> None:
    old = media_root / _media_name("a", ".jpg")
    new = media_root / _media_name("c", ".png")
    old.write_bytes(b"old")
    new.write_bytes(b"newfile")

    stale_manifest = build_media_manifest(media_root)
    # Simulate manifest from before `new` existed
    stale_manifest["files"] = [f for f in stale_manifest["files"] if f["name"] == old.name]

    app.dependency_overrides[get_current_user] = _techspec_user
    _use_db(memory_db)
    try:
        client = TestClient(app)
        res = client.post(
            "/techspec/media/backup.zip",
            files={"manifest": ("manifest.json", json.dumps(stale_manifest), "application/json")},
        )
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("application/zip")
        with zipfile.ZipFile(io.BytesIO(res.content), "r") as zf:
            names = zf.namelist()
            assert names == [new.name]
            assert zf.read(new.name) == b"newfile"
    finally:
        app.dependency_overrides.clear()


def test_post_backup_delta_no_new_files(media_root: Path, memory_db) -> None:
    (media_root / _media_name("a", ".jpg")).write_bytes(b"x")
    manifest = build_media_manifest(media_root)

    app.dependency_overrides[get_current_user] = _techspec_user
    _use_db(memory_db)
    try:
        client = TestClient(app)
        res = client.post(
            "/techspec/media/backup.zip",
            files={"manifest": ("manifest.json", json.dumps(manifest), "application/json")},
        )
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("application/json")
        data = res.json()
        assert data["new_files"] == 0
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def memory_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import models as _orm_models  # noqa: F401
    from app.db.base import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _use_db(db) -> None:
    def _db():
        yield db

    app.dependency_overrides[get_db] = _db


def test_stored_backup_name_accepts_video() -> None:
    assert is_stored_media_backup_filename(_media_name("a", ".mp4"))
    assert is_stored_media_backup_filename("care_memos/" + _media_name("b", ".jpg"))
    assert is_stored_media_backup_filename(_media_name("c", ".mov"))
    assert is_stored_media_backup_filename(_media_name("d", ".webm"))
    assert not is_stored_media_backup_filename("notes.txt")


def test_backup_keeps_video_larger_than_image_limit(media_root: Path) -> None:
    name = _media_name("e", ".mp4")
    path = media_root / name
    path.write_bytes(b"\0" * (MAX_BYTES + 1))
    found = iter_media_backup_paths(media_root)
    assert [p.name for p in found] == [name]
    assert path.stat().st_size > MAX_BYTES
    assert _MAX_RESTORE_UNCOMPRESSED >= 50 * 1024 * 1024


def test_missing_referenced_media_files(media_root: Path, memory_db) -> None:
    present = _media_name("a", ".jpg")
    (media_root / present).write_bytes(b"photo")
    video = _media_name("b", ".mp4")
    memo = "care_memos/" + _media_name("c", ".png")
    draft_photo = _media_name("d", ".webp")
    memory_db.add(Client(name="Аня", photo_1=f"/media/{present}", photo_2=f"/media/{draft_photo}"))
    memory_db.add(
        StudioTemplateVariable(
            key="route_video",
            kind="video",
            is_builtin=True,
            file_path=video,
            file_name="kak-nas-nayti.mp4",
            updated_at=datetime(2026, 6, 20, 14, 30),
        )
    )
    memory_db.add(
        CareMemo(
            name="Уход",
            file_path=memo,
            file_name="memo.png",
            mime="image/png",
            size=4,
            created_at=datetime(2026, 1, 15, 8, 0),
        )
    )
    user = User(username="master", display_name="Мастер", password_hash="x", role=UserRole.MASTER)
    memory_db.add(user)
    memory_db.flush()
    client = memory_db.query(Client).one()
    memory_db.add(
        VisitDraft(
            created_by_user_id=user.id,
            created_at=datetime(2026, 3, 2, 10, 0),
            performed_date=datetime(2026, 10, 21, 12, 0),
            client_id=client.id,
            form_json='{"photo_1": "/media/' + draft_photo + '"}',
        )
    )
    memory_db.commit()

    missing = missing_referenced_media_files(memory_db, media_root)
    names = [item["name"] for item in missing]
    assert names == [memo, draft_photo, video]
    assert present not in names
    assert media_refs_in_text(f"/media/{video}") == [video]

    by_name = {item["name"]: item for item in missing}
    memo_use = by_name[memo]["uses"][0]
    assert memo_use["kind"] == "памятка"
    assert memo_use["href"] == f"/clients/notifications#care-memo-{memo_use['record_id']}"
    assert memo_use["created_at"] == datetime(2026, 1, 15, 8, 0)

    photo_uses = by_name[draft_photo]["uses"]
    assert [use["kind"] for use in photo_uses] == ["черновик визита", "фото клиента"]
    assert photo_uses[0]["created_at"] == datetime(2026, 3, 2, 10, 0)
    assert photo_uses[0]["href"] == f"/master/visit/draft/{photo_uses[0]['record_id']}"
    assert photo_uses[1]["record_id"] == client.id
    assert photo_uses[1]["href"] == f"/clients/{client.id}"
    assert photo_uses[1]["created_at"] is None

    video_use = by_name[video]["uses"][0]
    assert video_use["kind"] == "видео маршрута"
    assert video_use["href"] == "/clients/notifications#route-video"
    assert video_use["created_at"] == datetime(2026, 6, 20, 14, 30)


def test_get_full_backup_zip(media_root: Path, memory_db) -> None:
    a = media_root / _media_name("a", ".jpg")
    b = media_root / _media_name("d", ".webp")
    a.write_bytes(b"a")
    b.write_bytes(b"b")

    app.dependency_overrides[get_current_user] = _techspec_user
    _use_db(memory_db)
    try:
        client = TestClient(app)
        res = client.get("/techspec/media/backup.zip")
        assert res.status_code == 200
        with zipfile.ZipFile(io.BytesIO(res.content), "r") as zf:
            assert sorted(zf.namelist()) == sorted([a.name, b.name])
    finally:
        app.dependency_overrides.clear()


def test_get_backup_zip_includes_mp4_and_missing_report(media_root: Path, memory_db) -> None:
    video = _media_name("f", ".mp4")
    (media_root / video).write_bytes(b"mp4-bytes")
    absent = _media_name("9", ".mp4")
    memory_db.add(
        StudioTemplateVariable(
            key="route_video",
            kind="video",
            is_builtin=True,
            file_path=absent,
        )
    )
    memory_db.commit()

    app.dependency_overrides[get_current_user] = _techspec_user
    _use_db(memory_db)
    try:
        client = TestClient(app)
        res = client.get("/techspec/media/backup.zip")
        assert res.status_code == 200
        with zipfile.ZipFile(io.BytesIO(res.content), "r") as zf:
            assert video in zf.namelist()
            assert zf.read(video) == b"mp4-bytes"
            assert MEDIA_MISSING_REPORT_NAME in zf.namelist()
            report = zf.read(MEDIA_MISSING_REPORT_NAME).decode("utf-8")
            assert absent in report
            assert "видео маршрута #" in report
            assert "/clients/notifications#route-video" in report
    finally:
        app.dependency_overrides.clear()


def test_restore_accepts_mp4_and_skips_missing_report(media_root: Path) -> None:
    video = _media_name("f", ".mp4")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(video, b"video-bytes")
        zf.writestr(MEDIA_MISSING_REPORT_NAME, "отчёт\n")
    app.dependency_overrides[get_current_user] = _techspec_user
    try:
        client = TestClient(app)
        res = client.post(
            "/techspec/media/restore",
            files={"archive": ("backup.zip", buf.getvalue(), "application/zip")},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["restored"] == 1
        assert body["skipped"] == 1
        assert (media_root / video).read_bytes() == b"video-bytes"
        assert not (media_root / MEDIA_MISSING_REPORT_NAME).exists()
    finally:
        app.dependency_overrides.clear()


def _entry(name: str, size: int) -> MediaBackupEntry:
    return MediaBackupEntry(name=name, size=size, mtime=0, path=Path(name))


def test_split_media_entries_into_parts_respects_limit() -> None:
    parts = split_media_entries_into_parts(
        [_entry("a", 10), _entry("b", 10), _entry("c", 10), _entry("d", 25)],
        max_part_bytes=25,
    )
    assert [[item.name for item in part] for part in parts] == [["a", "b"], ["c"], ["d"]]

    huge = split_media_entries_into_parts([_entry("big", 100), _entry("small", 5)], max_part_bytes=20)
    assert [[item.name for item in part] for part in huge] == [["big"], ["small"]]
    assert split_media_entries_into_parts([], max_part_bytes=10) == [[]]


def _zip_payload(content: bytes) -> tuple[set[str], list[zipfile.ZipInfo]]:
    with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
        return set(zf.namelist()), list(zf.infolist())


def test_get_backup_zip_parts_cover_files_and_report_is_in_first(media_root: Path, memory_db, monkeypatch) -> None:
    monkeypatch.setattr("app.media_store.MEDIA_BACKUP_PART_MAX_BYTES", 8)
    a = media_root / _media_name("a", ".jpg")
    b = media_root / _media_name("b", ".jpg")
    c = media_root / _media_name("c", ".jpg")
    a.write_bytes(b"1234")
    b.write_bytes(b"5678")
    c.write_bytes(b"90ab")
    absent = _media_name("9", ".mp4")
    memory_db.add(
        StudioTemplateVariable(
            key="route_video",
            kind="video",
            is_builtin=True,
            file_path=absent,
        )
    )
    memory_db.commit()

    app.dependency_overrides[get_current_user] = _techspec_user
    _use_db(memory_db)
    try:
        client = TestClient(app)
        first = client.get("/techspec/media/backup.zip?part=1")
        second = client.get("/techspec/media/backup.zip?part=2")
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.headers["x-lb-media-part"] == "1"
        assert second.headers["x-lb-media-part"] == "2"
        assert first.headers["x-lb-media-total-parts"] == "2"
        assert second.headers["x-lb-media-total-parts"] == "2"
        assert "part1-of-2" in first.headers["content-disposition"]
        assert "part2-of-2" in second.headers["content-disposition"]

        names1, infos1 = _zip_payload(first.content)
        names2, infos2 = _zip_payload(second.content)
        assert all(info.compress_type == zipfile.ZIP_STORED for info in infos1 + infos2)
        assert MEDIA_MISSING_REPORT_NAME in names1
        assert MEDIA_MISSING_REPORT_NAME not in names2
        files1 = names1 - {MEDIA_MISSING_REPORT_NAME}
        assert files1.isdisjoint(names2)
        assert files1 | names2 == {a.name, b.name, c.name}
        with zipfile.ZipFile(io.BytesIO(first.content), "r") as zf:
            assert absent in zf.read(MEDIA_MISSING_REPORT_NAME).decode("utf-8")

        missing_part = client.get("/techspec/media/backup.zip?part=3")
        assert missing_part.status_code == 404
        assert client.get("/techspec/media/backup.zip?part=0").status_code == 404

        whole = client.get("/techspec/media/backup.zip")
        assert whole.status_code == 200
        assert "x-lb-media-part" not in whole.headers
        whole_names, _infos = _zip_payload(whole.content)
        assert whole_names == {a.name, b.name, c.name, MEDIA_MISSING_REPORT_NAME}
    finally:
        app.dependency_overrides.clear()


def test_post_backup_delta_parts(media_root: Path, memory_db, monkeypatch) -> None:
    monkeypatch.setattr("app.media_store.MEDIA_BACKUP_PART_MAX_BYTES", 8)
    old = media_root / _media_name("a", ".jpg")
    b = media_root / _media_name("b", ".jpg")
    c = media_root / _media_name("c", ".jpg")
    d = media_root / _media_name("d", ".jpg")
    for path, payload in ((old, b"old1"), (b, b"newb"), (c, b"newc"), (d, b"newd")):
        path.write_bytes(payload)
    stale = {"files": [{"name": old.name, "size": 4, "mtime": 1}]}
    absent = _media_name("9", ".mp4")
    memory_db.add(
        StudioTemplateVariable(
            key="route_video",
            kind="video",
            is_builtin=True,
            file_path=absent,
        )
    )
    memory_db.commit()

    app.dependency_overrides[get_current_user] = _techspec_user
    _use_db(memory_db)
    try:
        client = TestClient(app)
        files = {"manifest": ("manifest.json", json.dumps(stale), "application/json")}
        first = client.post("/techspec/media/backup.zip?part=1", files=files)
        second = client.post("/techspec/media/backup.zip?part=2", files=files)
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.headers["x-lb-media-part"] == "1"
        assert second.headers["x-lb-media-part"] == "2"
        assert first.headers["x-lb-media-total-parts"] == "2"
        assert first.headers["x-lb-media-new-files"] == "3"
        assert int(first.headers["x-lb-media-new-bytes"]) == 12
        names1, _ = _zip_payload(first.content)
        names2, _ = _zip_payload(second.content)
        assert MEDIA_MISSING_REPORT_NAME in names1
        assert MEDIA_MISSING_REPORT_NAME not in names2
        files1 = names1 - {MEDIA_MISSING_REPORT_NAME}
        assert files1.isdisjoint(names2)
        assert files1 | names2 == {b.name, c.name, d.name}
        assert client.post("/techspec/media/backup.zip?part=3", files=files).status_code == 404

        implicit = client.post("/techspec/media/backup.zip", files=files)
        assert implicit.status_code == 200
        assert implicit.headers["x-lb-media-part"] == "1"
        assert implicit.headers["x-lb-media-total-parts"] == "2"
        implicit_names, _ = _zip_payload(implicit.content)
        assert implicit_names == names1
    finally:
        app.dependency_overrides.clear()


def test_restore_parts_in_sequence_restores_full_set(media_root: Path, memory_db, monkeypatch) -> None:
    monkeypatch.setattr("app.media_store.MEDIA_BACKUP_PART_MAX_BYTES", 8)
    a = media_root / _media_name("a", ".jpg")
    b = media_root / _media_name("b", ".jpg")
    c = media_root / _media_name("c", ".jpg")
    a.write_bytes(b"1234")
    b.write_bytes(b"5678")
    c.write_bytes(b"90ab")

    app.dependency_overrides[get_current_user] = _techspec_user
    _use_db(memory_db)
    try:
        client = TestClient(app)
        part1 = client.get("/techspec/media/backup.zip?part=1").content
        part2 = client.get("/techspec/media/backup.zip?part=2").content
        for path in (a, b, c):
            path.unlink()

        restored = 0
        for index, content in enumerate((part1, part2), start=1):
            res = client.post(
                "/techspec/media/restore",
                files={"archive": (f"lb-media-backup-part{index}.zip", content, "application/zip")},
            )
            assert res.status_code == 200
            restored += int(res.json()["restored"])
        assert restored == 3
        assert a.read_bytes() == b"1234"
        assert b.read_bytes() == b"5678"
        assert c.read_bytes() == b"90ab"
    finally:
        app.dependency_overrides.clear()
