"""Человекочитаемые подписи полей аудита (все сущности)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.setting_keys import (
    AUDIT_RETENTION_MONTHS,
    CALENDAR_DISPLAY_HOUR_FROM,
    CALENDAR_DISPLAY_HOUR_TO,
    DISPLAY_TIMEZONE,
    EDIT_WINDOW_DAYS,
    KIT_MAX_RESERVES_PER_KIT,
    MASTER_LEVEL_LABEL_JUNIOR,
    MASTER_LEVEL_LABEL_MIDDLE,
    MASTER_LEVEL_LABEL_SENIOR,
    SALON_CUT_PCT,
)

if TYPE_CHECKING:
    from app.audit import FieldChange

# Уже человекочитаемые подписи (бронь: мастера по услугам) — не менять.
_HUMAN_PREFIXES = ("Мастера ·", "Мастера (")

# Технические имена → подпись в колонке «Поле».
AUDIT_FIELD_LABELS: dict[str, str] = {
    # общие
    "created": "Создание",
    "status": "Статус",
    "comment": "Комментарий",
    "photo_1": "Фото 1",
    "photo_2": "Фото 2",
    "photo_3": "Фото 3",
    "client_id": "Клиент",
    "is_active": "Активен",
    "is_confirmed": "Подтверждён",
    "is_voided": "Аннулирован",
    "is_cancelled": "Отменён",
    "voided_at": "Дата аннулирования",
    "voided_by_user_id": "Кто аннулировал",
    "cancelled_at": "Дата отмены",
    "cancelled_by_user_id": "Кто отменил",
    "cancelled_reason": "Причина отмены",
    "name": "Название",
    "short_name": "Короткое название",
    "value": "Значение",
    "value_json": "Значение (JSON)",
    "details_json": "Параметры (JSON)",
    "password": "Пароль",
    # пользователь / сотрудник
    "username": "Логин",
    "display_name": "Имя",
    "phone": "Телефон",
    "roles": "Роли",
    "roles_summary": "Роли",
    "master_level": "Уровень мастера",
    "salon_cut_pct_override": "Доля салона (переопределение)",
    # клиент
    "telegram": "Telegram",
    "vk": "VK",
    "instagram": "Instagram",
    "other_contact": "Другой контакт",
    "age_group": "Возрастная группа",
    "source": "Источник",
    "source_other": "Источник (другое)",
    "birth_day": "День рождения",
    "birth_month": "Месяц рождения",
    "birth_year": "Год рождения",
    # бронь
    "planned_date": "Дата и время",
    "kind": "Тип брони",
    "quoted_price_text": "Ориентировочная цена",
    "deposit_amount": "Депозит",
    "planned_service_id": "Основная услуга",
    "planned_product_kind": "Тип продукта",
    "planned_services": "Услуги в брони",
    "visit_masters": "Мастера (на весь визит)",
    "sale_order_masters": "Мастера заказа (продажа)",
    "booking_masters_mode": "Назначение мастеров",
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
    "consultation_types_json": "Типы консультации",
    "other_text": "Другое (консультация)",
    "consultation_duration_on": "Своя длительность консультации",
    "consultation_duration_h": "Длительность консультации (часы)",
    "consultation_duration_m": "Длительность консультации (минуты)",
    "consultation_duration_minutes": "Длительность консультации (мин)",
    "consultation_master_id": "Мастер консультации",
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
    # визит
    "client_type": "Тип клиента",
    "performed_date": "Дата выполнения",
    "duration_minutes": "Длительность (мин)",
    "masters_scope": "Назначение мастеров",
    "same_master_shares_all_services": "Один мастер на все услуги",
    "amount_from_client": "Сумма от клиента",
    "cost_total": "Себестоимость",
    "profit_before_split": "Прибыль до распределения",
    "salon_profit": "Прибыль салона",
    "masters_pool": "Фонд мастеров",
    "services_summary": "Услуги (JSON)",
    "visit_masters_summary": "Мастера (JSON)",
    # консультация
    "consultation_date": "Дата консультации",
    "types_json": "Типы консультации",
    "service_id": "Услуга",
    "preliminary_cost_text": "Предварительная стоимость",
    "consultation_kind": "Тип консультации (каталог)",
    # комплект
    "sku": "Артикул",
    "title": "Название",
    "description": "Описание",
    "notes": "Заметки",
    "blank_type_de": "Тип прядей (DE)",
    "blank_type_se": "Тип прядей (SE)",
    "blanks_condition": "Состояние прядей",
    "pieces_total": "Прядей всего",
    "pieces_available": "Прядей доступно",
    "composition_json": "Состав (JSON)",
    "stock_price_total": "Складская цена",
    "cost_total": "Себестоимость",
    "discount_percent": "Скидка (%)",
    "author_external": "Автор (внешний)",
    "author_staff_ids": "Авторы (сотрудники)",
    # продажа продукта
    "sale_percent": "Процент продажи",
    "material_service_id": "Услуга (материал)",
    "material_grams": "Материал (граммы)",
    "material_description": "Описание материала",
    "material_kanekalon_grams": "Канекалон (г)",
    "material_kudri_grams": "Кудри (г)",
    "material_manual_cost": "Себестоимость материала",
    "material_mix_source": "Микс — источник",
    "material_mix_complexity": "Микс — сложность",
    "material_mix_cost_amount": "Микс — стоимость",
    "material_mix_bonus_user_id": "Микс — бонус сотруднику",
    "material_mix_bonus_amount": "Микс — сумма бонуса",
    "material_mix_standalone_grams": "Микс — граммы",
    "material_cost_review_pending": "Проверка себестоимости",
    "kit_id": "Комплект",
    "kit_pieces_sold": "Прядей продано",
    "kit_breakdown_json": "Разбор комплекта (JSON)",
    "kit_lines_json": "Строки комплекта (JSON)",
    "kit_description": "Описание комплекта",
    "rubber_description": "Описание хвоста",
    "rubber_price_override": "Цена хвоста (переопределение)",
    "other_description": "Описание (другое)",
    "other_cost": "Себестоимость (другое)",
    "studio_margin_amount": "Маржа студии",
    # работа с продуктами
    "scope": "Область работы",
    "client_payment_kind": "Тип оплаты клиента",
    "kanekalon_grams": "Канекалон (г)",
    "kudri_grams": "Кудри (г)",
    "materials_cost_total": "Себестоимость материалов",
    "extra_costs_amount": "Доп. расходы",
    "cost_total_amount": "Себестоимость итого",
    "master_profit_amount": "Доля мастера",
    "studio_profit_amount": "Доля студии",
    "profit_total_amount": "Прибыль итого",
    # расходы студии
    "date": "Дата",
    "subcategory_id": "Подкатегория",
    "amount": "Сумма",
    # график мастера
    "time_from": "Начало работы",
    "time_to": "Конец работы",
    "break_from": "Перерыв с",
    "break_to": "Перерыв до",
    # каталог услуг
    "estimated_duration_minutes": "Длительность (мин)",
    "price_junior_from": "Цена — младший (от)",
    "price_junior_to": "Цена — младший (до)",
    "price_middle_from": "Цена — мастер (от)",
    "price_middle_to": "Цена — мастер (до)",
    "price_senior_from": "Цена — старший (от)",
    "price_senior_to": "Цена — старший (до)",
    "kit_section_override": "Блок комплекта",
    "tail_section_override": "Блок хвоста",
    "material_description_override": "Описание материала",
    "retail_material_kanekalon": "Розница — канекалон",
    "retail_material_kudri": "Розница — кудри",
    "retail_material_mix": "Розница — микс",
    "show_kit_section": "Показывать блок комплекта",
    "show_tail_section": "Показывать блок хвоста",
    "show_material_description": "Показывать описание материала",
    "show_thermo_visit": "Показывать термо (визит)",
    # анкета
    "field_type": "Тип поля",
    "label": "Подпись",
    "required": "Обязательное",
    "sort_order": "Порядок",
    "placeholder": "Подсказка в поле",
    "help_text": "Пояснение",
    "options_json": "Варианты (JSON)",
    "min_value": "Мин. значение",
    "max_value": "Макс. значение",
    "visibility_json": "Видимость (JSON)",
}

SETTING_KEY_LABELS: dict[str, str] = {
    SALON_CUT_PCT: "Доля салона",
    KIT_MAX_RESERVES_PER_KIT: "Макс. резервов на комплект",
    CALENDAR_DISPLAY_HOUR_FROM: "Календарь — час начала",
    CALENDAR_DISPLAY_HOUR_TO: "Календарь — час окончания",
    MASTER_LEVEL_LABEL_JUNIOR: "Подпись уровня — младший",
    MASTER_LEVEL_LABEL_MIDDLE: "Подпись уровня — мастер",
    MASTER_LEVEL_LABEL_SENIOR: "Подпись уровня — старший",
    EDIT_WINDOW_DAYS: "Окно редактирования (дней)",
    AUDIT_RETENTION_MONTHS: "Хранение аудита (мес.)",
    DISPLAY_TIMEZONE: "Часовой пояс",
}

_JSON_FIELD_NAMES = frozenset(
    {
        "details_json",
        "services_summary",
        "visit_masters_summary",
        "composition_json",
        "options_json",
        "visibility_json",
        "value_json",
        "types_json",
        "kit_breakdown_json",
        "kit_lines_json",
        "visit_stock_kit_lines_json",
        "visit_stock_breakdown_json",
        "visit_extra_stock_breakdown_json",
        "sale_stock_kit_lines_json",
        "sale_stock_breakdown_json",
        "consultation_types_json",
    }
)


def setting_key_audit_label(setting_key: str) -> str:
    key = str(setting_key or "").strip()
    return SETTING_KEY_LABELS.get(key, key or "Настройка")


def audit_field_label(field_name: str) -> str:
    raw = str(field_name or "").strip()
    if not raw:
        return "—"
    if raw in AUDIT_FIELD_LABELS:
        return AUDIT_FIELD_LABELS[raw]
    for prefix in _HUMAN_PREFIXES:
        if raw.startswith(prefix):
            return raw
    return raw


def resolve_audit_field_name(
    field_name: str,
    *,
    log_table: str | None = None,
    entity_id: str | int | None = None,
) -> str:
    """Подпись поля с учётом типа журнала аудита."""
    raw = str(field_name or "").strip()
    if log_table == "setting_audit_logs" and raw == "value" and entity_id is not None:
        return setting_key_audit_label(str(entity_id))
    return audit_field_label(raw)


def audit_field_is_json(field_name: str) -> bool:
    raw = str(field_name or "").strip()
    if raw in _JSON_FIELD_NAMES:
        return True
    if raw.endswith("(JSON)"):
        return True
    return False


def apply_audit_field_labels(
    changes: list[Any],
    *,
    log_table: str | None = None,
    entity_id: str | int | None = None,
) -> list[Any]:
    from app.audit import FieldChange

    return [
        FieldChange(
            field_name=resolve_audit_field_name(
                ch.field_name,
                log_table=log_table,
                entity_id=entity_id,
            ),
            old_value=ch.old_value,
            new_value=ch.new_value,
        )
        for ch in changes
    ]


def planned_service_masters_audit_field_label(service_name: str, time_hm: str) -> str:
    """Подпись поля аудита для мастеров одной услуги в брони."""
    name = (service_name or "").strip() or "Услуга"
    if len(name) > 88:
        name = name[:87] + "…"
    return f"Мастера · {name} ({time_hm})"


def diff_planned_service_masters_audit(
    before: list[tuple[str, str, str]],
    after: list[tuple[str, str, str]],
) -> list[Any]:
    """Сравнить мастеров по услугам: (key, field_label, masters_csv)."""
    from app.audit import FieldChange

    before_map = {k: (label, masters) for k, label, masters in before}
    after_map = {k: (label, masters) for k, label, masters in after}
    keys = sorted(set(before_map) | set(after_map))
    out: list[Any] = []
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
