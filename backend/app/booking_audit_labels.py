"""Человекочитаемые подписи полей аудита брони."""

from __future__ import annotations

from app.audit import FieldChange

# Технические имена полей → подписи для колонки «Поле».
BOOKING_AUDIT_FIELD_LABELS: dict[str, str] = {
    "client_id": "Клиент",
    "planned_date": "Дата и время",
    "kind": "Тип брони",
    "status": "Статус",
    "quoted_price_text": "Ориентировочная цена",
    "deposit_amount": "Депозит",
    "photo_1": "Фото 1",
    "photo_2": "Фото 2",
    "photo_3": "Фото 3",
    "comment": "Комментарий",
    "planned_service_id": "Основная услуга",
    "planned_product_kind": "Тип продукта",
    "planned_services": "Услуги в брони",
    "visit_masters": "Мастера (на весь визит)",
    "sale_order_masters": "Мастера заказа (продажа)",
    "cancelled_reason": "Причина отмены",
    "cancelled_at": "Дата отмены",
    "cancelled_by_user_id": "Кто отменил",
    "details_json": "Параметры (JSON)",
    "booking_masters_mode": "Назначение мастеров",
    # details_json keys — визит
    "visit_custom_duration_on": "Своя длительность визита",
    "visit_custom_duration_h": "Длительность визита (часы)",
    "visit_custom_duration_m": "Длительность визита (минуты)",
    "visit_kit_mode": "Комплект для визита",
    "visit_stock_kit_id": "Комплект со склада",
    "visit_stock_kit_pieces": "Пряди со склада",
    "visit_stock_kit_lines_json": "Комплекты по услугам (склад)",
    "visit_stock_breakdown_json": "Разбор комплекта (склад)",
    "visit_stock_use_entire": "Весь комплект со склада",
    "visit_own_need_correction": "Нужна коррекция",
    "visit_own_need_extra_blanks": "Нужны доп. пряди",
    "visit_extra_blanks_mode": "Доп. пряди — источник",
    "visit_extra_stock_kit_id": "Доп. комплект со склада",
    "visit_extra_stock_kit_pieces": "Доп. пряди со склада",
    "visit_extra_stock_breakdown_json": "Разбор доп. комплекта",
    "visit_extra_stock_use_entire": "Весь доп. комплект со склада",
    "visit_order_blanks_qty": "Заказ прядей (кол-во)",
    "visit_order_blanks_desc": "Заказ прядей (описание)",
    "visit_order_use_masters": "Заказ прядей — через мастеров",
    "visit_order_master_ids": "Мастера для заказа прядей",
    "visit_extra_order_blanks_qty": "Доп. заказ прядей (кол-во)",
    "visit_extra_order_blanks_desc": "Доп. заказ прядей (описание)",
    "corr_wash": "Коррекция — мытьё",
    "corr_trim_qty": "Коррекция — подстрижка",
    "corr_hourly_hours": "Коррекция — почасовая работа",
    "corr_kit_description": "Коррекция — описание комплекта",
    "corr_kit_blanks_count": "Коррекция — прядей",
    "corr_steam": "Коррекция — пар",
    "corr_circle": "Коррекция — круг",
    "calc_product_min": "Калькулятор — продукт (мин)",
    "calc_product_max": "Калькулятор — продукт (макс)",
    "calc_service_min": "Калькулятор — услуга (мин)",
    "calc_service_max": "Калькулятор — услуга (макс)",
    # консультация
    "consultation_types_json": "Типы консультации",
    "other_text": "Другое (консультация)",
    "consultation_duration_on": "Своя длительность консультации",
    "consultation_duration_h": "Длительность консультации (часы)",
    "consultation_duration_m": "Длительность консультации (минуты)",
    "consultation_duration_minutes": "Длительность консультации (мин)",
    "consultation_master_id": "Мастер консультации",
    # продажа
    "product_kind": "Вид продажи",
    "sale_kit_mode": "Комплект — источник",
    "sale_stock_kit_id": "Комплект со склада (продажа)",
    "sale_stock_kit_lines_json": "Комплекты по услугам (продажа)",
    "sale_stock_kit_pieces": "Пряди (продажа)",
    "sale_stock_breakdown_json": "Разбор комплекта (продажа)",
    "sale_stock_use_entire": "Весь комплект (продажа)",
    "sale_kit_order_master_ids": "Мастера — заказ комплекта",
    "sale_order_blanks_qty": "Заказ прядей (продажа)",
    "sale_order_blanks_desc": "Заказ прядей — описание",
    "sale_rubber_mode": "Хвост/резинка — источник",
    "sale_rubber_order_master_id": "Мастер — заказ хвоста",
    "sale_rubber_type": "Тип хвоста/резинки",
}


def planned_service_masters_audit_field_label(service_name: str, time_hm: str) -> str:
    """Подпись поля аудита для мастеров одной услуги в брони."""
    name = (service_name or "").strip() or "Услуга"
    if len(name) > 88:
        name = name[:87] + "…"
    return f"Мастера · {name} ({time_hm})"


def booking_audit_field_label(field_name: str) -> str:
    raw = str(field_name or "").strip()
    if not raw:
        return "—"
    if raw in BOOKING_AUDIT_FIELD_LABELS:
        return BOOKING_AUDIT_FIELD_LABELS[raw]
    if raw.startswith("Мастера ·") or raw.startswith("Мастера ("):
        return raw
    return raw


def apply_booking_audit_field_labels(changes: list[FieldChange]) -> list[FieldChange]:
    return [
        FieldChange(
            field_name=booking_audit_field_label(ch.field_name),
            old_value=ch.old_value,
            new_value=ch.new_value,
        )
        for ch in changes
    ]


def diff_planned_service_masters_audit(
    before: list[tuple[str, str, str]],
    after: list[tuple[str, str, str]],
) -> list[FieldChange]:
    """Сравнить мастеров по услугам: (key, field_label, masters_csv)."""
    before_map = {k: (label, masters) for k, label, masters in before}
    after_map = {k: (label, masters) for k, label, masters in after}
    keys = sorted(set(before_map) | set(after_map))
    out: list[FieldChange] = []
    for key in keys:
        b = before_map.get(key)
        a = after_map.get(key)
        old_m = b[1] if b else "—"
        new_m = a[1] if a else "—"
        if old_m == new_m:
            continue
        label = (a[0] if a else b[0] if b else key)
        out.append(FieldChange(field_name=label, old_value=old_m, new_value=new_m))
    return out
