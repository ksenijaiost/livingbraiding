"""Подписи услуг для UI (каталог, визит, календарь)."""

from __future__ import annotations

from app.db.models import Service, VisitService

_ARROW = " → "


def format_duration_minutes_ru(minutes: int | None, *, default_minutes: int | None = None) -> str:
    """Человекочитаемая длительность: «2 ч», «2 ч 30 мин», «45 мин»."""
    m = int(minutes or 0)
    if m <= 0 and default_minutes is not None:
        m = int(default_minutes or 0)
    if m <= 0:
        return "—"
    h, mm = divmod(m, 60)
    parts: list[str] = []
    if h > 0:
        parts.append(f"{h} ч")
    if mm > 0:
        parts.append(f"{mm} мин")
    return " ".join(parts) if parts else "—"


def format_service_catalog_path(service: Service | None) -> str:
    if service is None:
        return ""
    sub = getattr(service, "subcategory", None)
    cat = getattr(sub, "category", None) if sub else None
    parts: list[str] = []
    if cat and (cat.name or "").strip():
        parts.append(str(cat.name).strip())
    if sub and (sub.name or "").strip():
        parts.append(str(sub.name).strip())
    if (service.name or "").strip():
        parts.append(str(service.name).strip())
    return _ARROW.join(parts) if parts else (service.name or "").strip()


def format_visit_service_catalog_path(vs: VisitService | None) -> str:
    if vs is None:
        return ""
    parts = [
        (vs.category_name or "").strip(),
        (vs.subcategory_name or "").strip(),
        (vs.service_name or "").strip(),
    ]
    parts = [p for p in parts if p]
    return _ARROW.join(parts) if parts else (vs.service_name or "").strip()


def booking_service_labels_from_booking(booking) -> str:
    """Пути услуг брони для таблицы календаря (через «; »)."""
    lines = list(getattr(booking, "planned_services", None) or [])
    if lines:
        paths: list[str] = []
        for ps in sorted(lines, key=lambda x: (int(x.sort_order or 0), int(x.id or 0))):
            svc = getattr(ps, "service", None)
            p = format_service_catalog_path(svc)
            if p:
                paths.append(p)
        if paths:
            return "; ".join(paths)
    svc = getattr(booking, "planned_service", None)
    return format_service_catalog_path(svc)
