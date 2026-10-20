"""Памятки-картинки: файлы, слоты «до»/«после» в каталоге, сбор по визиту/брони."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.engine import Connection
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

MemoKind = Literal["before", "after"]
MEMO_BEFORE: MemoKind = "before"
MEMO_AFTER: MemoKind = "after"

MODE_INHERIT = "inherit"
MODE_NONE = "none"
MODE_CUSTOM = "custom"

# Значения select в формах каталога.
SLOT_INHERIT = "__inherit__"
SLOT_NONE = "__none__"

CatalogEntity = ServiceCategory | ServiceSubcategory | Service


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


def _append_usage(
    out: list[dict[str, str]],
    *,
    kind: str,
    name: str,
    href: str,
    slot_label: str,
) -> None:
    out.append(
        {
            "kind": kind,
            "name": name,
            "href": href,
            "slot": slot_label,
        }
    )


def memo_usage(db: Session, memo_id: int) -> list[dict[str, str]]:
    mid = int(memo_id)
    out: list[dict[str, str]] = []
    for cat in db.scalars(
        select(ServiceCategory).where(
            (ServiceCategory.care_memo_before_id == mid) | (ServiceCategory.care_memo_after_id == mid)
        )
    ).all():
        href = f"/admin/catalog/categories/{int(cat.id)}/edit"
        if cat.care_memo_before_id == mid and (cat.care_memo_before_mode or "") == MODE_CUSTOM:
            _append_usage(out, kind="категория", name=cat.name, href=href, slot_label="до")
        if cat.care_memo_after_id == mid and (cat.care_memo_after_mode or "") == MODE_CUSTOM:
            _append_usage(out, kind="категория", name=cat.name, href=href, slot_label="после")
    for sub in db.scalars(
        select(ServiceSubcategory).where(
            (ServiceSubcategory.care_memo_before_id == mid)
            | (ServiceSubcategory.care_memo_after_id == mid)
        )
    ).all():
        href = f"/admin/catalog/subcategories/{int(sub.id)}/edit"
        if sub.care_memo_before_id == mid and (sub.care_memo_before_mode or "") == MODE_CUSTOM:
            _append_usage(out, kind="подкатегория", name=sub.name, href=href, slot_label="до")
        if sub.care_memo_after_id == mid and (sub.care_memo_after_mode or "") == MODE_CUSTOM:
            _append_usage(out, kind="подкатегория", name=sub.name, href=href, slot_label="после")
    for svc in db.scalars(
        select(Service).where(
            (Service.care_memo_before_id == mid) | (Service.care_memo_after_id == mid)
        )
    ).all():
        href = f"/admin/catalog/services/{int(svc.id)}/edit"
        if svc.care_memo_before_id == mid and (svc.care_memo_before_mode or "") == MODE_CUSTOM:
            _append_usage(out, kind="услуга", name=svc.name, href=href, slot_label="до")
        if svc.care_memo_after_id == mid and (svc.care_memo_after_mode or "") == MODE_CUSTOM:
            _append_usage(out, kind="услуга", name=svc.name, href=href, slot_label="после")
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


def _clear_entity_slot(entity: CatalogEntity, kind: MemoKind, memo_id: int) -> None:
    mode_attr = f"care_memo_{kind}_mode"
    id_attr = f"care_memo_{kind}_id"
    if getattr(entity, id_attr, None) != memo_id:
        return
    setattr(entity, id_attr, None)
    if (getattr(entity, mode_attr, None) or MODE_INHERIT) == MODE_CUSTOM:
        setattr(entity, mode_attr, MODE_INHERIT)


def _clear_links(db: Session, memo_id: int) -> None:
    mid = int(memo_id)
    for cat in db.scalars(
        select(ServiceCategory).where(
            (ServiceCategory.care_memo_before_id == mid) | (ServiceCategory.care_memo_after_id == mid)
        )
    ).all():
        _clear_entity_slot(cat, MEMO_BEFORE, mid)
        _clear_entity_slot(cat, MEMO_AFTER, mid)
    for sub in db.scalars(
        select(ServiceSubcategory).where(
            (ServiceSubcategory.care_memo_before_id == mid)
            | (ServiceSubcategory.care_memo_after_id == mid)
        )
    ).all():
        _clear_entity_slot(sub, MEMO_BEFORE, mid)
        _clear_entity_slot(sub, MEMO_AFTER, mid)
    for svc in db.scalars(
        select(Service).where(
            (Service.care_memo_before_id == mid) | (Service.care_memo_after_id == mid)
        )
    ).all():
        _clear_entity_slot(svc, MEMO_BEFORE, mid)
        _clear_entity_slot(svc, MEMO_AFTER, mid)


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
    text_value = str(raw or "").strip()
    if not text_value:
        return None
    try:
        mid = int(text_value)
    except (TypeError, ValueError) as e:
        raise ValueError("Памятка не найдена.") from e
    if db.get(CareMemo, mid) is None:
        raise ValueError("Памятка не найдена.")
    return mid


def parse_memo_slot_choice(db: Session, raw: object) -> tuple[str, int | None]:
    """Select: __inherit__ | __none__ | <id> → (mode, memo_id)."""
    text_value = str(raw or "").strip()
    if not text_value or text_value == SLOT_INHERIT:
        return MODE_INHERIT, None
    if text_value == SLOT_NONE:
        return MODE_NONE, None
    mid = parse_care_memo_id(db, text_value)
    if mid is None:
        return MODE_NONE, None
    return MODE_CUSTOM, mid


def apply_memo_slot(db: Session, entity: CatalogEntity, kind: MemoKind, raw: object) -> None:
    mode, mid = parse_memo_slot_choice(db, raw)
    setattr(entity, f"care_memo_{kind}_mode", mode)
    setattr(entity, f"care_memo_{kind}_id", mid)


def apply_category_memos(db: Session, category: ServiceCategory, before_raw: object, after_raw: object) -> None:
    apply_memo_slot(db, category, MEMO_BEFORE, before_raw)
    apply_memo_slot(db, category, MEMO_AFTER, after_raw)


def apply_entity_memos(db: Session, entity: ServiceSubcategory | Service, before_raw: object, after_raw: object) -> None:
    apply_memo_slot(db, entity, MEMO_BEFORE, before_raw)
    apply_memo_slot(db, entity, MEMO_AFTER, after_raw)


def slot_form_value(mode: str | None, memo_id: int | None) -> str:
    normalized = (mode or MODE_INHERIT).strip().lower()
    if normalized == MODE_NONE:
        return SLOT_NONE
    if normalized == MODE_CUSTOM and memo_id:
        return str(int(memo_id))
    return SLOT_INHERIT


def _memo_by_id(db: Session, memo_id: int | None) -> CareMemo | None:
    if not memo_id:
        return None
    return db.get(CareMemo, int(memo_id))


def _entity_slot(entity: CatalogEntity, kind: MemoKind) -> tuple[str, int | None]:
    mode = getattr(entity, f"care_memo_{kind}_mode", None) or MODE_INHERIT
    mid = getattr(entity, f"care_memo_{kind}_id", None)
    return str(mode).strip().lower(), int(mid) if mid else None


def resolve_category_memo(db: Session, cat: ServiceCategory | None, kind: MemoKind) -> CareMemo | None:
    if cat is None:
        return None
    mode, mid = _entity_slot(cat, kind)
    if mode in (MODE_INHERIT, MODE_NONE):
        return None
    if mode == MODE_CUSTOM:
        return _memo_by_id(db, mid)
    return None


def resolve_subcategory_memo(
    db: Session, sub: ServiceSubcategory | None, kind: MemoKind = MEMO_AFTER
) -> CareMemo | None:
    if sub is None:
        return None
    mode, mid = _entity_slot(sub, kind)
    if mode == MODE_NONE:
        return None
    if mode == MODE_CUSTOM:
        return _memo_by_id(db, mid)
    cat = db.get(ServiceCategory, int(sub.category_id))
    return resolve_category_memo(db, cat, kind)


def resolve_care_memo(
    db: Session, service: Service | None, kind: MemoKind = MEMO_AFTER
) -> CareMemo | None:
    """Итоговая памятка услуги: свой слот → подкатегория → категория."""
    if service is None:
        return None
    mode, mid = _entity_slot(service, kind)
    if mode == MODE_NONE:
        return None
    if mode == MODE_CUSTOM:
        return _memo_by_id(db, mid)
    sub = db.get(ServiceSubcategory, int(service.subcategory_id))
    return resolve_subcategory_memo(db, sub, kind)


def inherit_label_for_category(_db: Session, _cat: ServiceCategory, kind: MemoKind) -> str:
    del kind  # у категории «как у родителя» = нет
    return "нет"


def inherit_label_for_subcategory(db: Session, sub: ServiceSubcategory, kind: MemoKind) -> str:
    cat = db.get(ServiceCategory, int(sub.category_id))
    memo = resolve_category_memo(db, cat, kind)
    return memo.name if memo is not None else "нет"


def inherit_label_for_service(db: Session, service: Service, kind: MemoKind) -> str:
    sub = db.get(ServiceSubcategory, int(service.subcategory_id))
    memo = resolve_subcategory_memo(db, sub, kind)
    return memo.name if memo is not None else "нет"


def memo_form_context(db: Session, entity: CatalogEntity) -> dict[str, object]:
    """Контекст двух слотов для шаблонов каталога."""
    if isinstance(entity, ServiceCategory):
        before_inherit = inherit_label_for_category(db, entity, MEMO_BEFORE)
        after_inherit = inherit_label_for_category(db, entity, MEMO_AFTER)
    elif isinstance(entity, ServiceSubcategory):
        before_inherit = inherit_label_for_subcategory(db, entity, MEMO_BEFORE)
        after_inherit = inherit_label_for_subcategory(db, entity, MEMO_AFTER)
    else:
        before_inherit = inherit_label_for_service(db, entity, MEMO_BEFORE)
        after_inherit = inherit_label_for_service(db, entity, MEMO_AFTER)
    bm, bi = _entity_slot(entity, MEMO_BEFORE)
    am, ai = _entity_slot(entity, MEMO_AFTER)
    return {
        "care_memos": list_care_memos(db),
        "form_care_memo_before": slot_form_value(bm, bi),
        "form_care_memo_after": slot_form_value(am, ai),
        "care_memo_before_inherit_label": before_inherit,
        "care_memo_after_inherit_label": after_inherit,
    }


def resolve_visit_service_memo(
    db: Session, row: VisitService, kind: MemoKind = MEMO_AFTER
) -> CareMemo | None:
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
    return resolve_care_memo(db, svc, kind)


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


def care_memo_ids_for_visit(db: Session, visit: Visit, *, kind: MemoKind = MEMO_AFTER) -> list[int]:
    rows = list(
        db.scalars(
            select(VisitService)
            .where(VisitService.visit_id == int(visit.id), VisitService.is_cancelled.is_(False))
            .order_by(VisitService.sort_order.asc(), VisitService.id.asc())
        ).all()
    )
    return _unique_ids([resolve_visit_service_memo(db, row, kind) for row in rows])


def care_memo_ids_for_booking(
    db: Session, booking: Booking, *, kind: MemoKind = MEMO_AFTER
) -> list[int]:
    rows = list(
        db.scalars(
            select(BookingPlannedService)
            .where(BookingPlannedService.booking_id == int(booking.id))
            .order_by(BookingPlannedService.sort_order.asc(), BookingPlannedService.id.asc())
        ).all()
    )
    memos: list[CareMemo | None] = []
    for row in rows:
        memos.append(resolve_care_memo(db, db.get(Service, int(row.service_id)), kind))
    return _unique_ids(memos)


def memo_ids_if_enabled(
    db: Session, rule: object, *, visit: Visit | None = None, booking: Booking | None = None
) -> list[int]:
    if not bool(getattr(rule, "send_care_memo", True)):
        return []
    if visit is not None and visit.id is not None:
        return care_memo_ids_for_visit(db, visit, kind=MEMO_AFTER)
    if booking is not None and booking.id is not None:
        return care_memo_ids_for_booking(db, booking, kind=MEMO_AFTER)
    return []


def memo_names(db: Session, ids: list[int]) -> list[str]:
    names: list[str] = []
    for mid in ids:
        memo = db.get(CareMemo, int(mid))
        if memo is not None:
            names.append(memo.name)
    return names


def legacy_after_slot_from_old(
    *,
    memo_id: int | None,
    mode: str | None = None,
    is_category: bool = False,
) -> tuple[str, int | None]:
    """Перенос старой одной ссылки в слот «после»."""
    if is_category:
        if memo_id:
            return MODE_CUSTOM, int(memo_id)
        return MODE_NONE, None
    normalized = (mode or MODE_INHERIT).strip().lower()
    if normalized == MODE_INHERIT:
        return MODE_INHERIT, None
    if memo_id:
        return MODE_CUSTOM, int(memo_id)
    return MODE_NONE, None


def migrate_care_memo_before_after(connection: Connection) -> None:
    """Заполнить новые слоты из старых care_memo_id / care_memo_mode (колонки ещё есть)."""
    for rid, mid in connection.execute(text("SELECT id, care_memo_id FROM service_categories")).fetchall():
        mode, after_id = legacy_after_slot_from_old(memo_id=mid, is_category=True)
        connection.execute(
            text(
                """
                UPDATE service_categories
                SET care_memo_before_mode = :bm,
                    care_memo_before_id = NULL,
                    care_memo_after_mode = :am,
                    care_memo_after_id = :aid
                WHERE id = :rid
                """
            ),
            {"bm": MODE_INHERIT, "am": mode, "aid": after_id, "rid": int(rid)},
        )
    for table in ("service_subcategories", "services"):
        rows = connection.execute(
            text(f"SELECT id, care_memo_mode, care_memo_id FROM {table}")
        ).fetchall()
        for rid, old_mode, mid in rows:
            mode, after_id = legacy_after_slot_from_old(memo_id=mid, mode=old_mode, is_category=False)
            connection.execute(
                text(
                    f"""
                    UPDATE {table}
                    SET care_memo_before_mode = :bm,
                        care_memo_before_id = NULL,
                        care_memo_after_mode = :am,
                        care_memo_after_id = :aid
                    WHERE id = :rid
                    """
                ),
                {"bm": MODE_INHERIT, "am": mode, "aid": after_id, "rid": int(rid)},
            )


def downgrade_care_memo_before_after(connection: Connection) -> None:
    """Вернуть старые колонки из слота «после» (новые колонки ещё есть)."""
    for rid, mode, mid in connection.execute(
        text("SELECT id, care_memo_after_mode, care_memo_after_id FROM service_categories")
    ).fetchall():
        after_id = int(mid) if mid and (mode or "") == MODE_CUSTOM else None
        connection.execute(
            text("UPDATE service_categories SET care_memo_id = :mid WHERE id = :rid"),
            {"mid": after_id, "rid": int(rid)},
        )
    for table in ("service_subcategories", "services"):
        rows = connection.execute(
            text(f"SELECT id, care_memo_after_mode, care_memo_after_id FROM {table}")
        ).fetchall()
        for rid, mode, mid in rows:
            normalized = (mode or MODE_INHERIT).strip().lower()
            if normalized == MODE_INHERIT:
                old_mode, old_id = MODE_INHERIT, None
            elif normalized == MODE_NONE:
                old_mode, old_id = MODE_CUSTOM, None
            else:
                old_mode, old_id = MODE_CUSTOM, int(mid) if mid else None
            connection.execute(
                text(
                    f"""
                    UPDATE {table}
                    SET care_memo_mode = :mode, care_memo_id = :mid
                    WHERE id = :rid
                    """
                ),
                {"mode": old_mode, "mid": old_id, "rid": int(rid)},
            )
