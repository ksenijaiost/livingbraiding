from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.datastructures import UploadFile

MANIFEST_VERSION = 1


ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
}
MAX_BYTES = 10 * 1024 * 1024  # 10MB

VIDEO_EXTS = {".mp4", ".mov", ".webm"}
VIDEO_MIME = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}

# Имена как у save_upload_*: <32 hex>.<ext> в корне или в care_memos/.
# HEIC на диске не остаётся (конвертируется в JPG). Видео — mp4/mov/webm в корне.
# Лимит картинок MAX_BYTES (10 МБ) к отбору файлов бэкапа не применяется.
_BACKUP_EXTS = "jpg|jpeg|png|webp|mp4|mov|webm"
_BACKUP_STORED_NAME_RE = re.compile(
    rf"^(?:care_memos/)?[a-f0-9]{{32}}\.(?:{_BACKUP_EXTS})$",
    re.IGNORECASE,
)
_MEDIA_REF_RE = re.compile(
    rf"(?<![a-f0-9])((?:care_memos/)?[a-f0-9]{{32}}\.(?:{_BACKUP_EXTS}))(?![a-z0-9])",
    re.IGNORECASE,
)
_BACKUP_ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".webm"}
CARE_MEMO_BACKUP_DIR = "care_memos"
# Отчёт «ссылка в БД есть, файла на диске нет» — в корне zip; restore его пропускает.
MEDIA_MISSING_REPORT_NAME = "lb-media-missing.txt"


def is_stored_media_backup_filename(name: str) -> bool:
    normalized = (name or "").replace("\\", "/").strip()
    return bool(_BACKUP_STORED_NAME_RE.match(normalized)) and Path(normalized).suffix.lower() in _BACKUP_ALLOWED_EXTS


def media_backup_arcname(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def iter_media_backup_paths(root: Path) -> list[Path]:
    found: list[Path] = []
    if not root.is_dir():
        return found
    found.extend(p for p in root.iterdir() if p.is_file() and is_stored_media_backup_filename(p.name))
    sub = root / CARE_MEMO_BACKUP_DIR
    if sub.is_dir():
        found.extend(p for p in sub.iterdir() if p.is_file() and is_stored_media_backup_filename(p.name))
    return sorted(found, key=lambda p: media_backup_arcname(root, p))


@dataclass(frozen=True, slots=True)
class MediaBackupEntry:
    name: str
    size: int
    mtime: int
    path: Path


def iter_media_backup_entries(root: Path) -> list[MediaBackupEntry]:
    out: list[MediaBackupEntry] = []
    for p in iter_media_backup_paths(root):
        st = p.stat()
        out.append(
            MediaBackupEntry(
                name=media_backup_arcname(root, p),
                size=int(st.st_size),
                mtime=int(st.st_mtime),
                path=p,
            )
        )
    return out


def build_media_manifest(root: Path) -> dict[str, Any]:
    entries = iter_media_backup_entries(root)
    total_bytes = sum(e.size for e in entries)
    return {
        "version": MANIFEST_VERSION,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "file_count": len(entries),
        "total_bytes": total_bytes,
        "files": [{"name": e.name, "size": e.size, "mtime": e.mtime} for e in entries],
    }


def parse_media_manifest(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Манифест должен быть JSON-объектом.")
    files_raw = raw.get("files")
    if not isinstance(files_raw, list):
        raise ValueError("В манифесте нет списка files.")
    files: list[dict[str, Any]] = []
    for item in files_raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not is_stored_media_backup_filename(name):
            continue
        try:
            size = int(item.get("size"))
        except (TypeError, ValueError):
            size = -1
        try:
            mtime = int(item.get("mtime"))
        except (TypeError, ValueError):
            mtime = 0
        files.append({"name": name, "size": size, "mtime": mtime})
    return {"files": files}


def filter_entries_missing_from_manifest(
    entries: list[MediaBackupEntry],
    manifest: dict[str, Any],
) -> list[MediaBackupEntry]:
    """Файлы на сервере, которых нет в сохранённом манифесте (сверка по name)."""
    known: set[str] = set()
    for item in manifest.get("files") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if is_stored_media_backup_filename(name):
            known.add(name)
    return [e for e in entries if e.name not in known]


def media_refs_in_text(raw: str | None) -> list[str]:
    """Относительные пути хранилища внутри URL `/media/…`, голого имени или JSON."""
    text = str(raw or "").replace("\\/", "/").replace("\\", "/")
    found: list[str] = []
    seen: set[str] = set()
    for match in _MEDIA_REF_RE.finditer(text):
        rel = match.group(1).lower()
        if rel in seen or not is_stored_media_backup_filename(rel):
            continue
        seen.add(rel)
        found.append(rel)
    return found


def missing_referenced_media_files(db: object, root: Path | None = None) -> list[str]:
    """
    Файлы, на которые есть ссылка в БД, но которых нет в каталоге загрузок.
    Фото, памятки, видео маршрута и пути `/media/…` в черновиках.
    """
    from sqlalchemy import select

    from app.db.models import (
        Booking,
        CareMemo,
        Client,
        Consultation,
        Kit,
        StudioTemplateVariable,
        Visit,
        VisitDraft,
        WorkDraft,
    )

    columns = (
        (Client, "photo_1"),
        (Client, "photo_2"),
        (Kit, "photo_1"),
        (Booking, "photo_1"),
        (Booking, "photo_2"),
        (Booking, "photo_3"),
        (Consultation, "photo_1"),
        (Consultation, "photo_2"),
        (Visit, "photo_1"),
        (Visit, "photo_2"),
        (Visit, "photo_3"),
        (CareMemo, "file_path"),
        (StudioTemplateVariable, "file_path"),
        (StudioTemplateVariable, "value"),
        (VisitDraft, "form_json"),
        (WorkDraft, "form_json"),
    )
    refs: set[str] = set()
    for model, attr in columns:
        col = getattr(model, attr)
        for value in db.scalars(select(col).where(col.is_not(None))):  # type: ignore[attr-defined]
            refs.update(media_refs_in_text(None if value is None else str(value)))
    base = (root or media_root_dir()).resolve()
    missing: list[str] = []
    for rel in sorted(refs):
        path = (base / rel).resolve()
        try:
            path.relative_to(base)
        except ValueError:
            missing.append(rel)
            continue
        if not path.is_file():
            missing.append(rel)
    return missing


def media_backup_stats() -> dict[str, int | str]:
    """Статистика файлов, попадающих в /techspec/media/backup.zip."""
    root = media_root_dir().resolve()
    root.mkdir(parents=True, exist_ok=True)
    files = iter_media_backup_paths(root)
    other_files = sum(
        1 for p in root.iterdir() if p.is_file() and not is_stored_media_backup_filename(p.name)
    )
    total_bytes = sum(int(p.stat().st_size) for p in files)
    return {
        "file_count": len(files),
        "total_bytes": total_bytes,
        "skipped_other_files": other_files,
        "media_root": str(root),
    }


def media_root_dir() -> Path:
    # Under backend/data/uploads (same server as code).
    root = Path(os.environ.get("LB_MEDIA_ROOT") or "data/uploads")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_ext(filename: str | None, content_type: str | None) -> str | None:
    name = (filename or "").strip()
    ext = Path(name).suffix.lower() if name else ""
    if ext in ALLOWED_EXTS:
        return ext
    # fallback by content-type
    ct = (content_type or "").strip().lower()
    if ct == "image/jpeg":
        return ".jpg"
    if ct == "image/png":
        return ".png"
    if ct == "image/webp":
        return ".webp"
    if ct in ("image/heic", "image/heif"):
        return ".heic"
    return None


def _is_upload_file_like(v: object) -> bool:
    if isinstance(v, UploadFile):
        return True
    return hasattr(v, "read") and hasattr(v, "filename") and hasattr(v, "content_type")


def get_nonempty_upload(form: object, name: str) -> object | None:
    """
    Extract the first non-empty file upload for `name` from a Starlette `FormData`.

    Some clients/proxies may yield compatible upload objects that are not the exact
    `UploadFile` class; we accept UploadFile and simple duck-typed upload objects.
    """
    items: list[object] = []
    if hasattr(form, "getlist"):
        try:
            items = list(form.getlist(name))  # type: ignore[assignment]
        except Exception:
            items = []
    if not items and hasattr(form, "get"):
        v = form.get(name)  # type: ignore[assignment]
        if v is not None:
            items = [v]
    for it in items:
        if not _is_upload_file_like(it):
            continue
        fn = getattr(it, "filename", None)
        if not (str(fn or "").strip()):
            continue
        return it
    return None


async def save_upload_image(upload: object) -> str:
    """
    Save an uploaded image to local disk and return a DB-safe URL: `/media/<rel_path>`.
    """
    ext = _safe_ext(upload.filename, upload.content_type)
    if not ext:
        raise ValueError("Некорректный файл: допускаются JPG/PNG/WebP/HEIC.")

    # yyyy-mm style folders are overkill; keep it simple for now.
    heic_out_ext = ".jpg" if ext in (".heic", ".heif") else ext
    rel_name = f"{uuid.uuid4().hex}{heic_out_ext}"
    root = media_root_dir()
    dest = (root / rel_name)

    # HEIC/HEIF: конвертируем в JPG перед сохранением.
    if ext in (".heic", ".heif"):
        # Read upload into memory (лимит 10MB уже стоит).
        data_parts: list[bytes] = []
        total = 0
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError("Файл слишком большой (лимит 10MB).")
            data_parts.append(chunk)
        data = b"".join(data_parts)

        try:
            from io import BytesIO

            from PIL import Image  # type: ignore
            from pillow_heif import register_heif_opener  # type: ignore

            register_heif_opener()
            img = Image.open(BytesIO(data))
            img.load()

            # HEIC может содержать прозрачность; JPEG её не поддерживает.
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")

            img.save(dest, format="JPEG", quality=90, optimize=True)
        except ImportError as e:
            raise ValueError("Для HEIC нужна библиотека pillow-heif.") from e
        except Exception as e:
            # Do not leak internals to UI; keep message actionable.
            raise ValueError("Не удалось обработать HEIC-фото.") from e

        # reset for potential reuse
        try:
            await upload.seek(0)
        except Exception:
            pass
        return f"/media/{rel_name}"

    # JPG/PNG/WebP: сохраняем как есть (стримом).
    total = 0
    with dest.open("wb") as f:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                try:
                    f.close()
                    dest.unlink(missing_ok=True)
                except Exception:
                    pass
                raise ValueError("Файл слишком большой (лимит 10MB).")
            f.write(chunk)

    # reset for potential reuse
    try:
        await upload.seek(0)
    except Exception:
        pass

    return f"/media/{rel_name}"


def _video_ext(filename: str | None) -> str | None:
    ext = Path(filename or "").suffix.lower()
    if ext in VIDEO_EXTS:
        return ext
    return None


async def save_upload_video(upload: object, *, max_bytes: int) -> tuple[str, str, int]:
    """Сохранить видео в каталог загрузок. Возвращает (имя файла, исходное имя, размер)."""
    ext = _video_ext(getattr(upload, "filename", None))
    if not ext:
        raise ValueError("Видео: допускаются файлы mp4, mov и webm.")
    limit = max(1, int(max_bytes))
    rel_name = f"{uuid.uuid4().hex}{ext}"
    dest = media_root_dir() / rel_name
    original = Path(str(getattr(upload, "filename", None) or "video")).name[:200] or "video"
    total = 0
    with dest.open("wb") as f:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                try:
                    f.close()
                    dest.unlink(missing_ok=True)
                except Exception:
                    pass
                mb = max(1, limit // (1024 * 1024))
                raise ValueError(f"Видео слишком большое (лимит {mb} МБ).")
            f.write(chunk)
    if total <= 0:
        dest.unlink(missing_ok=True)
        raise ValueError("Файл видео пустой.")
    try:
        await upload.seek(0)
    except Exception:
        pass
    return rel_name, original, total


def delete_media_file(rel_name: str | None) -> None:
    """Удалить файл из каталога загрузок по относительному имени (без каталогов)."""
    name = Path(str(rel_name or "")).name
    if not name or name in (".", ".."):
        return
    try:
        (media_root_dir() / name).unlink(missing_ok=True)
    except Exception:
        return


async def replace_or_clear_photo(form: object, current: str | None, field: str) -> tuple[str | None, bool]:
    """Удалить текущее фото (clear_<field>) или заменить файлом. Возвращает (url, изменилось)."""
    from app.forms_parse import parse_bool

    url = current or None
    changed = False
    if parse_bool(getattr(form, "get", lambda _k, _d=None: None)(f"clear_{field}")):
        delete_media_by_url(url)
        url = None
        changed = True
    upload = get_nonempty_upload(form, field)
    if upload is not None:
        new_url = await save_upload_image(upload)
        if url:
            delete_media_by_url(url)
        url = new_url
        changed = True
    return url, changed


def delete_media_by_url(url: str | None) -> None:
    """
    Best-effort delete of a previously stored local media URL.
    Only deletes if it looks like our `/media/<name>` url.
    """
    if not url:
        return
    u = str(url).strip()
    if not u.startswith("/media/"):
        return
    rel = u[len("/media/") :].strip().lstrip("/")
    if not rel:
        return
    p = media_root_dir() / rel
    try:
        p.unlink(missing_ok=True)
    except Exception:
        return
