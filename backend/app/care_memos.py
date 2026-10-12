"""Памятки-картинки после визита: файлы, привязка в каталоге, сбор по визиту."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    Booking,
    BookingPlannedService,
    CareMemo,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    Visit,
    VisitService,
)
from app.display_time import format_naive_utc_datetime, get_display_timezone
from app.media_store import MAX_BYTES, media_root_dir
from app.time_utils import utcnow_naive

CARE_SUBDIR = "care_memos"
_ALLOWED = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
MODE_INHERIT = "inherit"
MODE_CUSTOM = "custom"


class CareMemoInUse(Exception):
    """Удаление нужно подтвердить: памятка ещё привязана."""

    def __init__(self, links: list[dict[str, str]]) -> None:
        self.links = links
        super().__init__("Точно ли хотите удалить?")


def parse_memo_name(raw: object) -> str:
    name = " ".join(str(raw or "").split())
    if not name:
        raise ValueError("Укажите кодовое название.")
    if len(name) > 80:
        raise ValueError("Название не длиннее 80 символов.")
    return name


def list_care_memos(db: Session) -> list[CareMemo]:
    return list(db.scalars(select(CareMemo).order_by(CareMemo.name.asc(), CareMemo.id.asc())).all())


def care_memos_for_ui(db: Session) -> list[dict[str, object]]:
    tz_name = get_display_timezone(db)
    out: list[dict[str, object]] = []
    for row in list_care_memos(db):
        out.append(
            {
                "id": int(row.id),
                "name": row.name,
                "file_name": row.file_name,
                "preview_url": f"/media/{row.file_path}",
                "created_label": format_naive_utc_datetime(row.created_at, tz_name),
            }
        )
    return out


def memo_delete_preview(db: Session, memo_id: int) -> dict[str, object] | None:
    memo = db.get(CareMemo, int(memo_id))
    if memo is None:
        return None
    links = memo_usage(db, int(memo.id))
    if not links:
        return None
    return {"id": int(memo.id), "name": memo.name, "links": links}


def memo_usage(db: Session, memo_id: int) -> list[dict[str, str]]:
    mid = int(memo_id)
    out: list[dict[str, str]] = []
    categories = db.scalars(select(ServiceCategory).where(ServiceCategory.care_memo_id == mid)).all()
    for cat in categories:
        out.append(
            {
                "kind": "категория",
                "name": cat.name,
                "href": f"/admin/catalog/categories/{int(cat.id)}/edit",
            }
        )
    subs = db.scalars(select(ServiceSubcategory).where(ServiceSubcategory.care_memo_id == mid)).all()
    for sub in subs:
        out.append(
            {
                "kind": "подкатегория",
                "name": sub.name,
                "href": f"/admin/catalog/subcategories/{int(sub.id)}/edit",
            }
        )
    services = db.scalars(select(Service).where(Service.care_memo_id == mid)).all()
    for svc in services:
        out.append(
            {
                "kind": "услуга",
                "name": svc.name,
                "href": f"/admin/catalog/services/{int(svc.id)}/edit",
            }
        )
    return out


def _name_taken(db: Session, name: str, *, exclude_id: int | None = None) -> bool:
    key = name.casefold()
    for row in db.scalars(select(CareMemo)).all():
        if exclude_id is not None and int(row.id) == int(exclude_id):
            continue
        if (row.name or "").casefold() == key:
            return True
    return False


def _stored_ext(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext == ".jpeg":
        ext = ".jpg"
    if ext not in (".jpg", ".png", ".webp"):
        raise ValueError("Допускаются JPG, PNG и WebP.")
    return ext


def store_care_memo_bytes(data: bytes, filename: str) -> tuple[str, str, str, int]:
    ext = _stored_ext(filename)
    if not data:
        raise ValueError("Пустой файл.")
    if len(data) > MAX_BYTES:
        raise ValueError("Файл больше 10 МБ.")
    rel = f"{CARE_SUBDIR}/{uuid.uuid4().hex}{ext}"
    dest = media_root_dir() / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    original = Path(filename).name or f"memo{ext}"
    mime = _ALLOWED.get(Path(filename).suffix.lower(), "application/octet-stream")
    if Path(filename).suffix.lower() == ".jpeg":
        mime = "image/jpeg"
    return rel, original[:255], mime, len(data)


def _unlink_rel(rel: str | None) -> None:
    if not rel:
        return
    root = media_root_dir().resolve()
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


async def _read_upload(upload: object) -> tuple[bytes, str]:
    filename = str(getattr(upload, "filename", "") or "")
    read = getattr(upload, "read", None)
    if read is None:
        raise ValueError("Выберите файл картинки.")
    data = read()
    if hasattr(data, "__await__"):
        data = await data
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("Выберите файл картинки.")
    return bytes(data), filename


async def create_care_memo(db: Session, *, name: object, upload: object | None) -> CareMemo:
    parsed = parse_memo_name(name)
    if upload is None:
        raise ValueError("Выберите файл картинки.")
    if _name_taken(db, parsed):
        raise ValueError("Памятка с таким названием уже есть.")
    data, filename = await _read_upload(upload)
    rel, file_name, mime, size = store_care_memo_bytes(data, filename)
    now = utcnow_naive()
    row = CareMemo(
        name=parsed,
        file_path=rel,
        file_name=file_name,
        mime=mime,
        size=size,
        created_at=now,
        updated_at=now,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as e:
        _unlink_rel(rel)
        raise ValueError("Памятка с таким названием уже есть.") from e
    return row


def rename_care_memo(db: Session, memo_id: int, name: object) -> CareMemo:
    memo = db.get(CareMemo, int(memo_id))
    if memo is None:
        raise ValueError("Памятка не найдена.")
    parsed = parse_memo_name(name)
    if _name_taken(db, parsed, exclude_id=int(memo.id)):
        raise ValueError("Памятка с таким названием уже есть.")
    memo.name = parsed
    memo.updated_at = utcnow_naive()
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError as e:
        raise ValueError("Памятка с таким названием уже есть.") from e
    return memo


async def replace_care_memo_file(db: Session, memo_id: int, upload: object | None) -> CareMemo:
    memo = db.get(CareMemo, int(memo_id))
    if memo is None:
        raise ValueError("Памятка не найдена.")
    if upload is None:
        raise ValueError("Выберите файл картинки.")
    data, filename = await _read_upload(upload)
    rel, file_name, mime, size = store_care_memo_bytes(data, filename)
    old = memo.file_path
    memo.file_path = rel
    memo.file_name = file_name
    memo.mime = mime
    memo.size = size
    memo.vk_attachment = None
    memo.max_image_token = None
    memo.telegram_file_id = None
    memo.updated_at = utcnow_naive()
    db.flush()
    if old != rel:
        _unlink_rel(old)
    return memo


def _clear_links(db: Session, memo_id: int) -> None:
    mid = int(memo_id)
    for cat in db.scalars(select(ServiceCategory).where(ServiceCategory.care_memo_id == mid)).all():
        cat.care_memo_id = None
    for sub in db.scalars(select(ServiceSubcategory).where(ServiceSubcategory.care_memo_id == mid)).all():
        sub.care_memo_id = None
        if (sub.care_memo_mode or MODE_INHERIT) == MODE_CUSTOM:
            sub.care_memo_mode = MODE_INHERIT
    for svc in db.scalars(select(Service).where(Service.care_memo_id == mid)).all():
        svc.care_memo_id = None
        if (svc.care_memo_mode or MODE_INHERIT) == MODE_CUSTOM:
            svc.care_memo_mode = MODE_INHERIT


def delete_care_memo(db: Session, memo_id: int, *, confirmed: bool) -> None:
    memo = db.get(CareMemo, int(memo_id))
    if memo is None:
        raise ValueError("Памятка не найдена.")
    links = memo_usage(db, int(memo.id))
    if links and not confirmed:
        raise CareMemoInUse(links)
    rel = memo.file_path
    _clear_links(db, int(memo.id))
    db.delete(memo)
    db.flush()
    _unlink_rel(rel)


def parse_care_memo_id(db: Session, raw: object) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        mid = int(text)
    except (TypeError, ValueError) as e:
        raise ValueError("Памятка не найдена.") from e
    if db.get(CareMemo, mid) is None:
        raise ValueError("Памятка не найдена.")
    return mid


def apply_category_memo(db: Session, category: ServiceCategory, raw: object) -> None:
    category.care_memo_id = parse_care_memo_id(db, raw)


def apply_care_memo_choice(db: Session, entity: ServiceSubcategory | Service, mode_raw: object, id_raw: object) -> None:
    mode = str(mode_raw or MODE_INHERIT).strip().lower()
    if mode not in (MODE_INHERIT, MODE_CUSTOM):
        mode = MODE_INHERIT
    entity.care_memo_mode = mode
    if mode == MODE_INHERIT:
        entity.care_memo_id = None
    else:
        entity.care_memo_id = parse_care_memo_id(db, id_raw)


def _memo_by_id(db: Session, memo_id: int | None) -> CareMemo | None:
    if not memo_id:
        return None
    return db.get(CareMemo, int(memo_id))


def resolve_subcategory_memo(db: Session, sub: ServiceSubcategory | None) -> CareMemo | None:
    if sub is None:
        return None
    if (sub.care_memo_mode or MODE_INHERIT) == MODE_CUSTOM:
        return _memo_by_id(db, sub.care_memo_id)
    cat = db.get(ServiceCategory, int(sub.category_id))
    if cat is None:
        return None
    return _memo_by_id(db, cat.care_memo_id)


def resolve_care_memo(db: Session, service: Service | None) -> CareMemo | None:
    """Итоговая памятка услуги: своя, иначе подкатегория, иначе категория."""
    if service is None:
        return None
    if (service.care_memo_mode or MODE_INHERIT) == MODE_CUSTOM:
        return _memo_by_id(db, service.care_memo_id)
    sub = db.get(ServiceSubcategory, int(service.subcategory_id))
    return resolve_subcategory_memo(db, sub)


def inherit_label_for_subcategory(db: Session, sub: ServiceSubcategory) -> str:
    cat = db.get(ServiceCategory, int(sub.category_id))
    memo = _memo_by_id(db, cat.care_memo_id) if cat is not None else None
    return memo.name if memo is not None else "нет"


def inherit_label_for_service(db: Session, service: Service) -> str:
    sub = db.get(ServiceSubcategory, int(service.subcategory_id))
    memo = resolve_subcategory_memo(db, sub)
    return memo.name if memo is not None else "нет"


def resolve_visit_service_memo(db: Session, row: VisitService) -> CareMemo | None:
    svc = db.get(Service, int(row.service_id)) if row.service_id else None
    if svc is None:
        svc = db.scalar(
            select(Service)
            .join(ServiceSubcategory, Service.subcategory_id == ServiceSubcategory.id)
            .join(ServiceCategory, ServiceSubcategory.category_id == ServiceCategory.id)
            .where(
                Service.name == row.service_name,
                ServiceSubcategory.name == row.subcategory_name,
                ServiceCategory.name == row.category_name,
            )
            .limit(1)
        )
    return resolve_care_memo(db, svc)


def _unique_ids(memos: list[CareMemo | None]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for memo in memos:
        if memo is None or memo.id is None:
            continue
        mid = int(memo.id)
        if mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
    return out


def care_memo_ids_for_visit(db: Session, visit: Visit) -> list[int]:
    rows = list(
        db.scalars(
            select(VisitService)
            .where(VisitService.visit_id == int(visit.id), VisitService.is_cancelled.is_(False))
            .order_by(VisitService.sort_order.asc(), VisitService.id.asc())
        ).all()
    )
    return _unique_ids([resolve_visit_service_memo(db, row) for row in rows])


def care_memo_ids_for_booking(db: Session, booking: Booking) -> list[int]:
    rows = list(
        db.scalars(
            select(BookingPlannedService)
            .where(BookingPlannedService.booking_id == int(booking.id))
            .order_by(BookingPlannedService.sort_order.asc(), BookingPlannedService.id.asc())
        ).all()
    )
    memos: list[CareMemo | None] = []
    for row in rows:
        memos.append(resolve_care_memo(db, db.get(Service, int(row.service_id))))
    return _unique_ids(memos)


def memo_ids_if_enabled(db: Session, rule: object, *, visit: Visit | None = None, booking: Booking | None = None) -> list[int]:
    if not bool(getattr(rule, "send_care_memo", True)):
        return []
    if visit is not None and visit.id is not None:
        return care_memo_ids_for_visit(db, visit)
    if booking is not None and booking.id is not None:
        return care_memo_ids_for_booking(db, booking)
    return []


def memo_names(db: Session, ids: list[int]) -> list[str]:
    names: list[str] = []
    for mid in ids:
        memo = db.get(CareMemo, int(mid))
        if memo is not None:
            names.append(memo.name)
    return names
