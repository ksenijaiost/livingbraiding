from __future__ import annotations

from pathlib import Path

import pytest

from app.media_store import media_backup_stats
from app.techspec_home import collect_techspec_home_stats


def test_media_backup_stats_counts_only_stored_files(tmp_path, monkeypatch) -> None:
    root = tmp_path / "uploads"
    root.mkdir()
    (root / ("a" * 32 + ".jpg")).write_bytes(b"x" * 100)
    (root / ("b" * 32 + ".png")).write_bytes(b"y" * 200)
    (root / ".gitkeep").write_text("")
    (root / "notes.txt").write_text("skip")

    monkeypatch.setenv("LB_MEDIA_ROOT", str(root))
    stats = media_backup_stats()

    assert stats["file_count"] == 2
    assert stats["total_bytes"] == 300
    assert stats["skipped_other_files"] == 2
    assert Path(str(stats["media_root"])).resolve() == root.resolve()


@pytest.fixture()
def memory_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import models as _orm_models  # noqa: F401
    from app.db.base import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def test_collect_techspec_home_stats_counts(memory_db) -> None:
    stats = collect_techspec_home_stats(memory_db)
    counts = stats["counts"]
    for key in (
        "users_active",
        "clients",
        "visits",
        "bookings",
        "consultations",
        "kits",
        "kits_active",
        "works",
        "product_sales",
        "catalog_products",
        "catalog_products_active",
        "services",
        "services_active",
    ):
        assert key in counts
        assert isinstance(counts[key], int)
        assert counts[key] >= 0
    assert "total_size_human" in stats["media"]
