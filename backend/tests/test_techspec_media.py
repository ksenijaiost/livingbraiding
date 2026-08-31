from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.auth import AuthUser, get_current_user
from app.db.models import UserRole
from app.main import app
from app.media_store import (
    build_media_manifest,
    filter_entries_missing_from_manifest,
    iter_media_backup_entries,
    parse_media_manifest,
)


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


def test_post_backup_delta_zip(media_root: Path) -> None:
    old = media_root / _media_name("a", ".jpg")
    new = media_root / _media_name("c", ".png")
    old.write_bytes(b"old")
    new.write_bytes(b"newfile")

    stale_manifest = build_media_manifest(media_root)
    # Simulate manifest from before `new` existed
    stale_manifest["files"] = [f for f in stale_manifest["files"] if f["name"] == old.name]

    app.dependency_overrides[get_current_user] = _techspec_user
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


def test_post_backup_delta_no_new_files(media_root: Path) -> None:
    (media_root / _media_name("a", ".jpg")).write_bytes(b"x")
    manifest = build_media_manifest(media_root)

    app.dependency_overrides[get_current_user] = _techspec_user
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


def test_get_full_backup_zip(media_root: Path) -> None:
    a = media_root / _media_name("a", ".jpg")
    b = media_root / _media_name("d", ".webp")
    a.write_bytes(b"a")
    b.write_bytes(b"b")

    app.dependency_overrides[get_current_user] = _techspec_user
    try:
        client = TestClient(app)
        res = client.get("/techspec/media/backup.zip")
        assert res.status_code == 200
        with zipfile.ZipFile(io.BytesIO(res.content), "r") as zf:
            assert sorted(zf.namelist()) == sorted([a.name, b.name])
    finally:
        app.dependency_overrides.clear()
