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


def booking_list_detail_parts(
    booking,
    *,
    linked_work=None,
    linked_visit=None,
    linked_sale=None,
    product_kind_label_fn=None,
) -> list[str]:
    """Краткие строки для колонки «Детали» в списке броней."""
    from app.db.models import BookingKind, ProductSaleKind, WorkKind, WorkScope

    parts: list[str] = []

    if linked_work is not None:
        scope = "в наличие" if linked_work.scope == WorkScope.IN_STOCK else "на заказ"
        kind_map = {
            WorkKind.KIT: "комплект/заготовки",
            WorkKind.MIX: "смешка",
            WorkKind.RUBBER: "хвосты/резинки",
            WorkKind.KIT_CORRECTION: "коррекция комплекта",
            WorkKind.OTHER: "другое",
            WorkKind.HAIR_EXT_PREP: "подготовка к наращиванию",
        }
        kind_l = kind_map.get(linked_work.kind, linked_work.kind.value)
        parts.append(f"Работа: {kind_l} ({scope})")

    kind = getattr(booking, "kind", None)
    if kind == BookingKind.VISIT:
        visit_path = ""
        if linked_visit is not None:
            services = sorted(getattr(linked_visit, "services", None) or [], key=lambda s: int(s.id or 0))
            if services:
                visit_path = format_visit_service_catalog_path(services[0])
            else:
                visit_path = "Визит"
        else:
            visit_path = booking_service_labels_from_booking(booking)
        if visit_path:
            parts.append(visit_path)
    elif kind == BookingKind.PRODUCT_SALE:
        if linked_sale is not None:
            sk = linked_sale.kind
            sale_map = {
                ProductSaleKind.KIT: "комплект",
                ProductSaleKind.RUBBER: "хвост/резинка",
                ProductSaleKind.MATERIAL: "материал",
                ProductSaleKind.OTHER: "другое",
            }
            parts.append(f"Продажа: {sale_map.get(sk, sk.value)}")
        else:
            pk = (getattr(booking, "planned_product_kind", None) or "").strip()
            if pk and product_kind_label_fn is not None:
                label = product_kind_label_fn(pk)
                if label and label != "—":
                    parts.append(f"Продажа: {label}")
    return parts
