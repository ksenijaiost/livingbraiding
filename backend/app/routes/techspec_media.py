"""
Бэкап и восстановление локальных загрузок (LB_MEDIA_ROOT / data/uploads).

Доступ только у пользователей с ролью TECHSPEC (см. require_techspec_user).
"""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.background import BackgroundTask

from app.auth import AuthUser, require_techspec_user
from app.media_store import (
    MediaBackupEntry,
    build_media_manifest,
    filter_entries_missing_from_manifest,
    is_stored_media_backup_filename,
    iter_media_backup_entries,
    iter_media_backup_paths,
    media_root_dir,
    parse_media_manifest,
)

router = APIRouter(prefix="/techspec/media", tags=["techspec-media"])

_MAX_RESTORE_UNCOMPRESSED = int(os.environ.get("LB_MEDIA_RESTORE_MAX_BYTES", str(1200 * 1024 * 1024)))
_MAX_UPLOAD_ZIP = int(os.environ.get("LB_MEDIA_RESTORE_MAX_ZIP_BYTES", str(1024 * 1024 * 1024)))
_MAX_RESTORE_FILES = int(os.environ.get("LB_MEDIA_RESTORE_MAX_FILES", "20000"))

# Служебные файлы в каталоге uploads (не попадают в бэкап; при restore пропускаются).
_SKIP_RESTORE_BASENAMES = frozenset({".gitkeep", ".ds_store", "thumbs.db", "desktop.ini"})


def _should_skip_restore_basename(base: str) -> bool:
    low = base.lower()
    if low in _SKIP_RESTORE_BASENAMES:
        return True
    if base.startswith("."):
        return True
    return False


def _unlink_quiet(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _safe_arc_basename(member: str) -> str | None:
    n = member.replace("\\", "/").strip()
    if not n or n.endswith("/"):
        return None
    parts = [p for p in n.split("/") if p]
    if len(parts) != 1:
        return None
    base = parts[0]
    if not base or base in (".", ".."):
        return None
    return base


def _build_backup_zip(paths: list[Path]) -> str:
    fd, tmp_path = tempfile.mkstemp(prefix="lb-media-", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in paths:
                zf.write(p, arcname=p.name)
    except Exception:
        _unlink_quiet(tmp_path)
        raise HTTPException(status_code=500, detail="Не удалось собрать архив.")
    return tmp_path


def _estimate_zip_bytes(entries: list[MediaBackupEntry]) -> int:
    return sum(e.size for e in entries)


@router.get("/manifest.json")
def techspec_media_manifest(
    current_user: Annotated[AuthUser, Depends(require_techspec_user())],
):
    """Скачать полный манифест файлов медиа-бэкапа."""
    root = media_root_dir().resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest = build_media_manifest(root)
    body = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    fname = f"lb-media-manifest-{date.today().isoformat()}.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/backup.zip")
def techspec_media_backup_zip_get(
    current_user: Annotated[AuthUser, Depends(require_techspec_user())],
):
    """Скачать архив всех файлов из каталога загрузок (только файлы в корне каталога)."""
    root = media_root_dir().resolve()
    root.mkdir(parents=True, exist_ok=True)
    paths = iter_media_backup_paths(root)
    tmp_path = _build_backup_zip(paths)
    fname = f"lb-media-backup-{date.today().isoformat()}.zip"
    return FileResponse(
        tmp_path,
        filename=fname,
        media_type="application/zip",
        background=BackgroundTask(_unlink_quiet, tmp_path),
    )


@router.post("/backup.zip")
async def techspec_media_backup_zip_post(
    current_user: Annotated[AuthUser, Depends(require_techspec_user())],
    manifest: UploadFile = File(..., description="Сохранённый manifest.json — в архив попадут только новые файлы"),
):
    """Инкрементальный бэкап: zip только файлов, которых нет в загруженном манифесте."""
    if not manifest.filename or not str(manifest.filename).lower().endswith(".json"):
        raise HTTPException(status_code=400, detail="Ожидается manifest.json")

    raw_bytes = await manifest.read()
    if len(raw_bytes) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Файл манифеста слишком большой.")
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Некорректный JSON в манифесте.")

    try:
        parsed_manifest = parse_media_manifest(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    root = media_root_dir().resolve()
    root.mkdir(parents=True, exist_ok=True)
    entries = iter_media_backup_entries(root)
    missing = filter_entries_missing_from_manifest(entries, parsed_manifest)

    if not missing:
        return JSONResponse(
            {
                "ok": True,
                "new_files": 0,
                "new_bytes": 0,
                "message": "Новых файлов нет — манифест актуален. Скачайте свежий manifest.json при необходимости.",
            }
        )

    est_bytes = _estimate_zip_bytes(missing)
    if est_bytes > _MAX_UPLOAD_ZIP:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Слишком много новых файлов (~{est_bytes // (1024 * 1024)} МБ). "
                "Скачайте полный backup.zip или обновите манифест после промежуточной синхронизации."
            ),
        )

    paths = [e.path for e in missing]
    tmp_path = _build_backup_zip(paths)
    fname = f"lb-media-delta-{date.today().isoformat()}.zip"
    return FileResponse(
        tmp_path,
        filename=fname,
        media_type="application/zip",
        headers={
            "X-LB-Media-New-Files": str(len(missing)),
            "X-LB-Media-New-Bytes": str(est_bytes),
        },
        background=BackgroundTask(_unlink_quiet, tmp_path),
    )


@router.post("/restore")
async def techspec_media_restore_zip(
    current_user: Annotated[AuthUser, Depends(require_techspec_user())],
    archive: UploadFile = File(..., description="ZIP с файлами в корне (имена как у /media/…)"),
):
    """
    Распаковать ZIP в каталог загрузок: только корневые имена вида <uuid>.jpg|…;
    существующие файлы с тем же именем перезаписываются.
    """
    if not archive.filename or not str(archive.filename).lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Ожидается .zip")

    root = media_root_dir().resolve()
    root.mkdir(parents=True, exist_ok=True)

    fd, tmp_zip = tempfile.mkstemp(prefix="lb-restore-", suffix=".zip")
    os.close(fd)
    total_written = 0
    restored = 0
    skipped = 0
    try:
        chunk_size = 1024 * 1024
        with open(tmp_zip, "wb") as out:
            while True:
                chunk = await archive.read(chunk_size)
                if not chunk:
                    break
                total_written += len(chunk)
                if total_written > _MAX_UPLOAD_ZIP:
                    raise HTTPException(status_code=400, detail="Архив (файл загрузки) слишком большой.")
                out.write(chunk)

        with zipfile.ZipFile(tmp_zip, "r") as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if len(infos) > _MAX_RESTORE_FILES:
                raise HTTPException(status_code=400, detail="Слишком много файлов в архиве.")
            uncompressed = sum(int(i.file_size) for i in infos)
            if uncompressed > _MAX_RESTORE_UNCOMPRESSED:
                raise HTTPException(status_code=400, detail="Суммарный размер после распаковки слишком большой.")

            for info in infos:
                base = _safe_arc_basename(info.filename)
                if base is None:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Недопустимый путь в архиве (нужен один файл в корне): {info.filename!r}",
                    )
                if _should_skip_restore_basename(base):
                    skipped += 1
                    continue
                if not is_stored_media_backup_filename(base):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Недопустимое имя файла (ожидается <uuid>.jpg|jpeg|png|webp): {base!r}",
                    )

                dest = (root / base).resolve()
                try:
                    dest.relative_to(root)
                except ValueError:
                    raise HTTPException(status_code=400, detail="Обход каталога в архиве.")

                with zf.open(info, "r") as src, open(dest, "wb") as dst:
                    copied = 0
                    while True:
                        buf = src.read(chunk_size)
                        if not buf:
                            break
                        copied += len(buf)
                        if copied > _MAX_RESTORE_UNCOMPRESSED:
                            raise HTTPException(status_code=400, detail="Файл в архиве слишком большой.")
                        dst.write(buf)
                restored += 1
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Повреждённый или не ZIP-файл.")
    finally:
        _unlink_quiet(tmp_zip)

    return JSONResponse({"restored": restored, "skipped": skipped, "by_user": current_user.username})
