from __future__ import annotations

import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

logger = logging.getLogger(__name__)

MANIFEST_VERSION = 1
_MEDIA_RELEASE_KEY = "lb_media_release"
_MEDIA_RELEASE_READY = "lb_media_release_ready"
# Разовая чистка не трогает файлы, которые могли ещё загружаться.
ORPHAN_MIN_AGE_SECONDS = 3600


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
# Одна часть полного или дельта-архива. Крупный файл не режется и уходит своей частью.
MEDIA_BACKUP_PART_MAX_BYTES = int(os.environ.get("LB_MEDIA_BACKUP_PART_MAX_BYTES", str(300 * 1024 * 1024)))


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


def split_media_entries_into_parts(
    entries: list[MediaBackupEntry],
    max_part_bytes: int | None = None,
) -> list[list[MediaBackupEntry]]:
    """Разбить уже отсортированный список файлов на части около max_part_bytes.

    Пустой список даёт одну пустую часть. Файл крупнее лимита не теряется:
    он открывает свою часть, если текущая ещё пуста.
    """
    limit = MEDIA_BACKUP_PART_MAX_BYTES if max_part_bytes is None else int(max_part_bytes)
    if not entries:
        return [[]]
    chunks: list[list[MediaBackupEntry]] = []
    current: list[MediaBackupEntry] = []
    current_bytes = 0
    for entry in entries:
        if current and current_bytes + entry.size > limit:
            chunks.append(current)
            current = []
            current_bytes = 0
        current.append(entry)
        current_bytes += entry.size
    if current:
        chunks.append(current)
    return chunks


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


def _missing_use(kind: str, record_id: int, href: str, created_at: datetime | None) -> dict[str, Any]:
    return {
        "kind": kind,
        "record_id": int(record_id),
        "href": href,
        "created_at": created_at,
    }


def _remember_use(
    found: dict[str, list[dict[str, Any]]],
    seen: set[tuple[str, str, int]],
    rel: str,
    kind: str,
    record_id: int,
    href: str,
    created_at: datetime | None,
) -> None:
    key = (rel, kind, int(record_id))
    if key in seen:
        return
    seen.add(key)
    found.setdefault(rel, []).append(_missing_use(kind, record_id, href, created_at))


def _scan_media_columns(
    db: object,
    found: dict[str, list[dict[str, Any]]],
    seen: set[tuple[str, str, int]],
    *,
    model: type,
    kind: str,
    href: str,
    attrs: tuple[str, ...],
    date_attr: str | None,
) -> None:
    from sqlalchemy import or_, select

    columns = [model.id]  # type: ignore[attr-defined]
    if date_attr:
        columns.append(getattr(model, date_attr))
    columns.extend(getattr(model, name) for name in attrs)
    filters = [getattr(model, name).is_not(None) for name in attrs]
    for row in db.execute(select(*columns).where(or_(*filters))):  # type: ignore[attr-defined]
        record_id = int(row.id)
        created = getattr(row, date_attr) if date_attr else None
        page = href.format(id=record_id)
        for name in attrs:
            for rel in media_refs_in_text(getattr(row, name)):
                _remember_use(found, seen, rel, kind, record_id, page, created)


def _scan_studio_variables(
    db: object,
    found: dict[str, list[dict[str, Any]]],
    seen: set[tuple[str, str, int]],
) -> None:
    from sqlalchemy import or_, select

    from app.db.models import StudioTemplateVariable

    stmt = select(
        StudioTemplateVariable.id,
        StudioTemplateVariable.kind,
        StudioTemplateVariable.updated_at,
        StudioTemplateVariable.file_path,
        StudioTemplateVariable.value,
    ).where(
        or_(
            StudioTemplateVariable.file_path.is_not(None),
            StudioTemplateVariable.value.is_not(None),
        )
    )
    for row in db.execute(stmt):  # type: ignore[attr-defined]
        kind = "видео маршрута" if row.kind == "video" else "переменная студии"
        href = "/clients/notifications#route-video" if row.kind == "video" else "/clients/notifications"
        for rel in media_refs_in_text(row.file_path) + media_refs_in_text(row.value):
            _remember_use(found, seen, rel, kind, int(row.id), href, row.updated_at)


def _collect_media_uses(db: object) -> dict[str, list[dict[str, Any]]]:
    from app.db.models import Booking, CareMemo, Client, Consultation, Kit, Visit, VisitDraft, WorkDraft

    found: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str, int]] = set()
    specs = (
        (Client, "фото клиента", "/clients/{id}", ("photo_1", "photo_2"), None),
        (Kit, "фото комплекта", "/kits/{id}", ("photo_1",), "created_at"),
        (Booking, "фото брони", "/bookings/{id}", ("photo_1", "photo_2", "photo_3"), "created_at"),
        (
            Consultation,
            "фото консультации",
            "/consultations/{id}",
            ("photo_1", "photo_2"),
            "created_at",
        ),
        (Visit, "фото визита", "/visits/{id}", ("photo_1", "photo_2", "photo_3"), "created_at"),
        (CareMemo, "памятка", "/clients/notifications#care-memo-{id}", ("file_path",), "created_at"),
        (VisitDraft, "черновик визита", "/master/visit/draft/{id}", ("form_json",), "created_at"),
        (WorkDraft, "черновик работы", "/sales/work/draft/{id}", ("form_json",), "created_at"),
    )
    for model, kind, href, attrs, date_attr in specs:
        _scan_media_columns(
            db,
            found,
            seen,
            model=model,
            kind=kind,
            href=href,
            attrs=attrs,
            date_attr=date_attr,
        )
    _scan_studio_variables(db, found, seen)
    return found


def referenced_media_rels(db: object) -> set[str]:
    """Относительные пути файлов, на которые есть ссылка в БД."""
    return set(_collect_media_uses(db))


def missing_referenced_media_files(db: object, root: Path | None = None) -> list[dict[str, Any]]:
    """
    Файлы, на которые есть ссылка в БД, но которых нет в каталоге загрузок.
    У каждого файла — все места: тип, id, страница и дата создания записи.
    Список отсортирован по дате (без даты — в конце).
    """
    found = _collect_media_uses(db)
    base = (root or media_root_dir()).resolve()
    missing: list[dict[str, Any]] = []
    for rel, uses in found.items():
        path = (base / rel).resolve()
        try:
            path.relative_to(base)
        except ValueError:
            on_disk = False
        else:
            on_disk = path.is_file()
        if on_disk:
            continue
        uses.sort(key=lambda use: (use["created_at"] is None, use["created_at"] or datetime.min, use["kind"], use["record_id"]))
        missing.append({"name": rel, "uses": uses})
    missing.sort(
        key=lambda item: (
            item["uses"][0]["created_at"] is None,
            item["uses"][0]["created_at"] or datetime.min,
            item["name"],
        )
    )
    return missing


def media_backup_stats() -> dict[str, Any]:
    """Статистика файлов, попадающих в /techspec/media/backup.zip."""
    root = media_root_dir().resolve()
    root.mkdir(parents=True, exist_ok=True)
    entries = iter_media_backup_entries(root)
    other_files = sum(
        1 for p in root.iterdir() if p.is_file() and not is_stored_media_backup_filename(p.name)
    )
    chunks = split_media_entries_into_parts(entries)
    parts = [
        {"part": idx, "file_count": len(chunk), "total_bytes": sum(e.size for e in chunk)}
        for idx, chunk in enumerate(chunks, start=1)
    ]
    return {
        "file_count": len(entries),
        "total_bytes": sum(e.size for e in entries),
        "skipped_other_files": other_files,
        "media_root": str(root),
        "parts": parts,
        "part_count": len(chunks),
    }


def media_root_dir() -> Path:
    # Under backend/data/uploads (same server as code).
    root = Path(os.environ.get("LB_MEDIA_ROOT") or "data/uploads")
    root.mkdir(parents=True, exist_ok=True)
    return root


def note_media_for_release(db: object | None, *raws: object) -> None:
    """Запомнить файлы: после успешного commit удалить те, на которые не осталось ссылок."""
    if db is None:
        return
    info = getattr(db, "info", None)
    if not isinstance(info, dict):
        return
    bucket: list[str] = info.setdefault(_MEDIA_RELEASE_KEY, [])
    for raw in raws:
        for rel in media_refs_in_text(None if raw is None else str(raw)):
            if rel not in bucket:
                bucket.append(rel)


def note_dropped_media_refs(db: object | None, before: object, after: object) -> None:
    """Ссылки, которые были в старом тексте и пропали в новом."""
    old = set(media_refs_in_text(None if before is None else str(before)))
    new = set(media_refs_in_text(None if after is None else str(after)))
    note_media_for_release(db, *(old - new))


def _unlink_stored_rel(rel: str) -> None:
    root = media_root_dir().resolve()
    path = (root / rel).resolve()
    path.relative_to(root)
    path.unlink(missing_ok=True)


def _plan_media_release(session: Session) -> None:
    """Пока транзакция ещё открыта: какие из отмеченных файлов уже нигде не числятся."""
    rels = list(session.info.get(_MEDIA_RELEASE_KEY) or [])
    if not rels:
        return
    try:
        referenced = referenced_media_rels(session)
    except Exception:
        logger.warning("Не удалось проверить ссылки на медиа перед удалением файлов", exc_info=True)
        session.info[_MEDIA_RELEASE_READY] = []
        return
    session.info[_MEDIA_RELEASE_READY] = [rel for rel in rels if rel not in referenced]


def _release_noted_media(session: Session) -> None:
    rels = list(session.info.pop(_MEDIA_RELEASE_READY, []) or [])
    session.info.pop(_MEDIA_RELEASE_KEY, None)
    for rel in rels:
        try:
            _unlink_stored_rel(rel)
        except Exception:
            logger.warning("Не удалось удалить медиафайл %s", rel, exc_info=True)


def _drop_noted_media(session: Session) -> None:
    if session.in_transaction():
        return
    session.info.pop(_MEDIA_RELEASE_KEY, None)
    session.info.pop(_MEDIA_RELEASE_READY, None)


event.listen(Session, "before_commit", _plan_media_release)
event.listen(Session, "after_commit", _release_noted_media)
event.listen(Session, "after_rollback", _drop_noted_media)


def unreferenced_media_files(db: object, root: Path | None = None) -> list[Path]:
    """Файлы каталога загрузок, на которые нет ссылки в БД. В выгрузку они всё равно входят."""
    base = (root or media_root_dir()).resolve()
    referenced = referenced_media_rels(db)
    found: list[Path] = []
    for path in iter_media_backup_paths(base):
        rel = media_backup_arcname(base, path).lower()
        if rel not in referenced:
            found.append(path)
    return found


def unreferenced_media_stats(db: object, root: Path | None = None) -> dict[str, int]:
    files = unreferenced_media_files(db, root)
    total = 0
    for path in files:
        try:
            total += int(path.stat().st_size)
        except OSError:
            continue
    return {"count": len(files), "total_bytes": total}


def _orphan_is_fresh(path: Path, *, now: float | None = None) -> bool:
    try:
        age = (now if now is not None else time.time()) - float(path.stat().st_mtime)
    except OSError:
        return True
    return age < ORPHAN_MIN_AGE_SECONDS


def delete_unreferenced_media(db: object, root: Path | None = None) -> tuple[int, int]:
    """Удалить файлы без ссылок старше часа. Возвращает (число, байты). Ошибки диска не пробрасывает."""
    deleted = 0
    freed = 0
    now = time.time()
    for path in unreferenced_media_files(db, root):
        if _orphan_is_fresh(path, now=now):
            continue
        try:
            size = int(path.stat().st_size)
            path.unlink(missing_ok=True)
        except Exception:
            logger.warning("Не удалось удалить лишний медиафайл %s", path, exc_info=True)
            continue
        deleted += 1
        freed += size
    return deleted, freed


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


async def replace_or_clear_photo(
    form: object,
    current: str | None,
    field: str,
    *,
    db: object | None = None,
) -> tuple[str | None, bool]:
    """Снять текущее фото (clear_<field>) или заменить файлом. Возвращает (url, изменилось).

    Старый файл удаляется после commit, если на него больше нет ссылок.
    """
    from app.forms_parse import parse_bool

    url = current or None
    changed = False
    if parse_bool(getattr(form, "get", lambda _k, _d=None: None)(f"clear_{field}")):
        note_media_for_release(db, url)
        url = None
        changed = True
    upload = get_nonempty_upload(form, field)
    if upload is not None:
        new_url = await save_upload_image(upload)
        if url:
            note_media_for_release(db, url)
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
