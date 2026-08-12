"""Технические данные для роли TECHSPEC."""

from __future__ import annotations

import re
import sys
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import (
    Booking,
    CatalogProduct,
    Client,
    Consultation,
    HourlyWorkEntry,
    Kit,
    ProductSale,
    Service,
    User,
    UserRoleAssignment,
    Visit,
    VisitDraft,
    WorkForInventory,
    WorkDraft,
    WorkPlan,
)
from app.display_time import get_display_timezone
from app.media_store import media_backup_stats
from app.settings import get_settings

_TABLE_DESCRIPTIONS: dict[str, str] = {
    "settings": "Настройки приложения и студии.",
    "users": "Пользователи системы.",
    "user_role_assignments": "Назначенные роли пользователей.",
    "user_audit_logs": "Аудит изменений пользователей.",
    "clients": "Карточки клиентов.",
    "client_thermo_templates": "Шаблоны термо-анкет по клиентам.",
    "studio_expense_categories": "Категории расходов студии.",
    "studio_expense_subcategories": "Подкатегории расходов студии.",
    "studio_expenses": "Записи о расходах студии.",
    "service_categories": "Категории услуг.",
    "category_questionnaire_fields": "Поля анкеты на уровне категории услуг.",
    "service_subcategories": "Подкатегории услуг.",
    "services": "Услуги прайса.",
    "subcategory_questionnaire_fields": "Поля анкеты на уровне подкатегории услуг.",
    "service_questionnaire_fields": "Поля анкеты на уровне конкретной услуги.",
    "material_prices_current": "Текущие цены материалов.",
    "kits": "Комплекты и заготовки.",
    "kit_blank_stock": "Склад заготовок комплектов.",
    "kit_reserves": "Резервы комплектов под брони и работы.",
    "kit_author_staff": "Авторы и участники по комплектам.",
    "catalog_products": "Товары прайса.",
    "bookings": "Брони клиентов.",
    "booking_planned_services": "Услуги, запланированные в бронях.",
    "booking_planned_service_masters": "Мастера по плановым услугам в бронях.",
    "booking_masters": "Мастера, назначенные на бронь.",
    "booking_staff": "Сотрудники, привязанные к броням.",
    "booking_audit_logs": "Аудит изменений броней.",
    "master_schedule_days": "Рабочие дни мастеров.",
    "master_time_blocks": "Блокировки времени в графике мастеров.",
    "master_schedule_audit_logs": "Аудит графика мастеров.",
    "consultations": "Проведенные консультации.",
    "consultation_services": "Услуги, обсужденные на консультациях.",
    "consultation_audit_logs": "Аудит изменений консультаций.",
    "product_sales": "Продажи товаров.",
    "work_for_inventory": "Работы с товарами и комплектами.",
    "work_for_inventory_staff": "Участники работ с товарами.",
    "work_rates": "Ставки и проценты для расчета работ.",
    "payroll_periods": "Периоды зарплатного учета.",
    "payroll_fund_ledger": "Журнал начислений, сторно и выплат.",
    "super_admin_purge_logs": "Логи удаления данных суперадмином.",
    "hourly_work_entries": "Записи почасовой работы.",
    "visits": "Проведенные визиты.",
    "visit_drafts": "Черновики визитов.",
    "visit_draft_participants": "Участники черновиков визитов.",
    "work_drafts": "Черновики работ с товарами.",
    "work_draft_participants": "Участники черновиков работ.",
    "visit_audit_logs": "Аудит изменений визитов.",
    "client_audit_logs": "Аудит изменений клиентов.",
    "kit_audit_logs": "Аудит изменений комплектов.",
    "product_sale_audit_logs": "Аудит изменений продаж товаров.",
    "studio_expense_audit_logs": "Аудит изменений расходов.",
    "work_for_inventory_audit_logs": "Аудит изменений работ с товарами.",
    "setting_audit_logs": "Аудит изменений настроек.",
    "work_rate_audit_logs": "Аудит изменений ставок.",
    "service_category_audit_logs": "Аудит категорий услуг.",
    "service_subcategory_audit_logs": "Аудит подкатегорий услуг.",
    "service_audit_logs": "Аудит услуг.",
    "category_questionnaire_field_audit_logs": "Аудит полей анкеты по категориям.",
    "subcategory_questionnaire_field_audit_logs": "Аудит полей анкеты по подкатегориям.",
    "service_questionnaire_field_audit_logs": "Аудит полей анкеты по услугам.",
    "visit_masters": "Мастера визитов.",
    "visit_services": "Услуги внутри визитов.",
    "visit_service_masters": "Мастера по конкретным услугам визита.",
    "visit_kit_usages": "Списания комплектов и материалов в визитах.",
    "work_plans": "Планы работ.",
}

_COUNT_MODELS: tuple[tuple[str, Any], ...] = (
    ("users_active", User),
    ("roles_assigned", UserRoleAssignment),
    ("clients", Client),
    ("visits", Visit),
    ("bookings", Booking),
    ("consultations", Consultation),
    ("kits", Kit),
    ("works", WorkForInventory),
    ("product_sales", ProductSale),
    ("hourly_work_entries", HourlyWorkEntry),
    ("work_plans", WorkPlan),
    ("visit_drafts", VisitDraft),
    ("work_drafts", WorkDraft),
    ("catalog_products", CatalogProduct),
    ("services", Service),
)

_READONLY_SQL_RE = re.compile(r"^\s*(?:--[^\n]*\n|\s|/\*.*?\*/)*", re.S)
_FORBIDDEN_SQL_TOKENS = (
    "insert",
    "update",
    "delete",
    "alter",
    "drop",
    "create",
    "grant",
    "revoke",
    "truncate",
    "vacuum",
    "analyze",
    "copy",
    "call",
    "do",
    "merge",
    "replace",
)


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} Б"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} КБ"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} МБ"
    return f"{n / (1024 * 1024 * 1024):.2f} ГБ"


def _database_label() -> str:
    url = make_url(get_settings().database_url)
    if url.drivername.startswith("sqlite"):
        db_path = url.database or ":memory:"
        return f"SQLite · {db_path}"
    host = url.host or "localhost"
    port = f":{url.port}" if url.port else ""
    db_name = url.database or ""
    return f"{url.drivername} · {host}{port}/{db_name}"


def _safe_table_description(name: str) -> str:
    return _TABLE_DESCRIPTIONS.get(name, "Служебная таблица приложения.")


def _count_rows(db: Session, model: Any, *, active_only: bool = False) -> int:
    stmt = select(func.count()).select_from(model)
    if model is User and active_only:
        stmt = stmt.where(User.is_active.is_(True))
    elif model is Visit:
        stmt = stmt.where(Visit.is_cancelled.is_(False))
    elif model is WorkForInventory:
        stmt = stmt.where(WorkForInventory.is_voided.is_(False))
    elif model is ProductSale:
        stmt = stmt.where(ProductSale.is_voided.is_(False))
    elif model is CatalogProduct and active_only:
        stmt = stmt.where(CatalogProduct.is_active.is_(True))
    elif model is Service and active_only:
        stmt = stmt.where(Service.is_active.is_(True))
    return int(db.scalar(stmt) or 0)


def collect_db_table_stats(db: Session) -> list[dict[str, Any]]:
    mapper_by_table = {
        mapper.local_table.name: mapper.class_
        for mapper in Base.registry.mappers
        if getattr(mapper, "local_table", None) is not None
    }
    rows: list[dict[str, Any]] = []
    tables = sorted(Base.metadata.tables.values(), key=lambda t: t.name)
    for idx, table in enumerate(tables, start=1):
        model = mapper_by_table.get(table.name)
        total = int(db.scalar(select(func.count()).select_from(table)) or 0)
        rows.append(
            {
                "idx": idx,
                "name": table.name,
                "description": _safe_table_description(table.name),
                "model_name": getattr(model, "__name__", None),
                "count": total,
            }
        )
    return rows


def _validate_readonly_sql(query: str) -> str:
    sql = (query or "").strip()
    if not sql:
        raise ValueError("SQL-запрос пуст.")

    normalized = sql.rstrip().rstrip(";").strip()
    if not normalized:
        raise ValueError("SQL-запрос пуст.")

    leading_stripped = _READONLY_SQL_RE.sub("", normalized).lstrip()
    first_word = leading_stripped.split(None, 1)[0].lower() if leading_stripped else ""
    if first_word not in {"select", "with", "explain"}:
        raise ValueError("Разрешены только SELECT / WITH / EXPLAIN.")

    lowered = re.sub(r"'(?:''|[^'])*'", "''", normalized.lower())
    for token in _FORBIDDEN_SQL_TOKENS:
        if re.search(rf"\b{re.escape(token)}\b", lowered):
            raise ValueError(f"Недопустимый SQL-оператор: {token.upper()}.")

    if ";" in normalized:
        raise ValueError("Разрешен только один SQL-запрос.")
    return normalized


def execute_readonly_sql(db: Session, query: str, *, row_limit: int = 200) -> dict[str, Any]:
    sql = _validate_readonly_sql(query)
    try:
        result = db.execute(text(sql))
        if not result.returns_rows:
            return {"sql": sql, "columns": [], "rows": [], "row_count": 0, "truncated": False}
        columns = list(result.keys())
        fetched = result.mappings().fetchmany(row_limit + 1)
        rows = [{k: row.get(k) for k in columns} for row in fetched[:row_limit]]
        return {
            "sql": sql,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": len(fetched) > row_limit,
        }
    except Exception:
        db.rollback()
        raise


def collect_techspec_home_stats(db: Session) -> dict[str, Any]:
    media = media_backup_stats()
    media["total_size_human"] = _fmt_bytes(int(media.get("total_bytes") or 0))
    counts = {key: _count_rows(db, model, active_only=(key in {"users_active", "catalog_products_active", "services_active"}))
              for key, model in _COUNT_MODELS}
    counts["kits_active"] = int(
        db.scalar(
            select(func.count()).select_from(Kit).where(
                Kit.is_active.is_(True),
                Kit.is_archived.is_(False),
            )
        )
        or 0
    )
    counts["catalog_products_active"] = _count_rows(db, CatalogProduct, active_only=True)
    counts["services_active"] = _count_rows(db, Service, active_only=True)

    py = sys.version_info
    settings = get_settings()

    return {
        "media": media,
        "database": _database_label(),
        "app_env": settings.app_env,
        "display_tz": get_display_timezone(db),
        "python_version": f"{py.major}.{py.minor}.{py.micro}",
        "counts": counts,
    }
