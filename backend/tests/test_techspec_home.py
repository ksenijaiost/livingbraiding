from __future__ import annotations

from pathlib import Path

from app.media_store import media_backup_stats


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
