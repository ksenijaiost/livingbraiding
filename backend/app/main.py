"""
FastAPI entrypoint: создание приложения, подключение роутеров, startup.

Шаблоны Jinja и фильтры/глобалы — в `app/webui.py`.
HTTP-роуты — в `app/routes/`.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

# Порядок импортов ниже сохранён осознанно: часть модулей импортирует друг друга,
# и менять порядок (в том числе алфавитный) без проверки не стоит.
from app.access_logging import AccessLogWithUserMiddleware, configure_request_access_logging
from app.admin_questionnaire_fields import router as admin_questionnaire_fields_router
from app.admin_service_catalog import router as admin_service_catalog_router
from app.audit_retention import purge_expired_audit_logs_startup_safe
from app.auth import SessionRememberRenewMiddleware
from app.bootstrap import ensure_initial_techspec_user
from app.db.session import SessionLocal
from app.display_time import DisplayTimezoneMiddleware
from app import booking_status_notify as _booking_status_notify  # noqa: F401 — слушатель смены статуса брони
from app.notification_worker import start_notification_worker, stop_notification_worker
from app.payroll_fund import backfill_all_visit_master_accruals_if_missing
from app.seed import ensure_dev_seed_data, ensure_prod_seed_data

from app import admin_studio_expenses as admin_studio_expenses_routes
from app import product_sales as product_sales_routes
from app import work_products as work_products_routes

from app.tech_error import SwallowClientDisconnectMiddleware, register_tech_error_handlers

from app.routes.visits import router as visits_router
from app.routes.consultations import router as consultations_router
from app.routes.settings import router as settings_router
from app.routes.clients import (
    legacy_clients_admin_router,
    router as clients_router,
)
from app.routes.bookings import (
    legacy_bookings_admin_router,
    legacy_master_bookings_page_router,
    master_mywork_page_router,
    router as bookings_router,
)
from app.routes.kits import (
    legacy_kits_admin_router,
    master_kits_router,
    router as kits_router,
)
from app.routes.reports import router as reports_router
from app.routes.master_statistics import router as master_statistics_router
from app.routes.work_plans import router as work_plans_router
from app.routes.hourly_work import router as hourly_work_router
from app.routes.payroll import router as payroll_router
from app.routes.staff import router as staff_router
from app.routes.me import router as me_router
from app.routes.telegram_webhook import router as telegram_webhook_router
from app.routes.vk_webhook import router as vk_webhook_router
from app.routes.max_webhook import router as max_webhook_router
from app.routes.notification_outbox import router as notification_outbox_router
from app.routes.studio_notifications import router as studio_notifications_router
from app.routes.master_visit import router as master_visit_router
from app.routes.master_clients import router as master_clients_router
from app.routes.calendar_api import router as calendar_api_router
from app.routes.products_catalog import router as products_catalog_router
from app.routes.products_calc import router as products_calc_router
from app.routes.public_pages import router as public_pages_router
from app.routes.help import router as help_router
from app.routes.media import router as media_router
from app.routes.techspec_data import router as techspec_data_router
from app.routes.techspec_media import router as techspec_media_router
from app.routes.super_admin_purge import router as super_admin_purge_router
from app.routes.master_schedule import router as master_schedule_router
from app.routes.auth_routes import router as auth_routes_router


def _run_startup_seed() -> None:
    """Access-лог (время, user) + optional dev seed + audit retention."""
    configure_request_access_logging()
    db = SessionLocal()
    try:
        try:
            db.execute(text("SELECT 1 FROM settings LIMIT 1"))
        except (OperationalError, ProgrammingError):
            return
        enable_dev_seed = os.environ.get("ENABLE_DEV_SEED", "").strip().lower() in ("1", "true", "yes")
        enable_prod_seed = os.environ.get("ENABLE_PROD_SEED", "").strip().lower() in ("1", "true", "yes")
        if enable_dev_seed:
            ensure_dev_seed_data(db)
        elif enable_prod_seed:
            ensure_prod_seed_data(db)
        ensure_initial_techspec_user(db)
        purge_expired_audit_logs_startup_safe(db)
        backfill_all_visit_master_accruals_if_missing(db)
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.max_http import max_ca_bundle_path

    max_ca_bundle_path()
    _run_startup_seed()
    worker = start_notification_worker()
    try:
        yield
    finally:
        await stop_notification_worker(worker)


# Порядок подключения важен: при совпадении путей побеждает роутер, подключённый раньше.
# Список повторяет прежний порядок `include_router` в этом файле.
ROUTERS = (
    admin_service_catalog_router,
    admin_questionnaire_fields_router,
    admin_studio_expenses_routes.router,
    product_sales_routes.router,
    product_sales_routes.legacy_admin_router,
    work_products_routes.router,
    work_products_routes.legacy_admin_router,
    visits_router,
    consultations_router,
    settings_router,
    clients_router,
    legacy_clients_admin_router,
    bookings_router,
    legacy_bookings_admin_router,
    master_mywork_page_router,
    legacy_master_bookings_page_router,
    kits_router,
    legacy_kits_admin_router,
    master_kits_router,
    reports_router,
    master_statistics_router,
    work_plans_router,
    hourly_work_router,
    payroll_router,
    staff_router,
    me_router,
    telegram_webhook_router,
    vk_webhook_router,
    max_webhook_router,
    notification_outbox_router,
    studio_notifications_router,
    master_visit_router,
    master_clients_router,
    calendar_api_router,
    products_catalog_router,
    products_calc_router,
    public_pages_router,
    help_router,
    media_router,
    techspec_data_router,
    techspec_media_router,
    super_admin_purge_router,
    master_schedule_router,
    auth_routes_router,
)

app = FastAPI(title="livingbraiding", lifespan=lifespan)
register_tech_error_handlers(app)
app.add_middleware(AccessLogWithUserMiddleware)
app.add_middleware(DisplayTimezoneMiddleware)
app.add_middleware(SessionRememberRenewMiddleware)
app.add_middleware(SwallowClientDisconnectMiddleware)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
for _router in ROUTERS:
    app.include_router(_router)


@app.get("/health")
def health() -> dict[str, bool]:
    """Проверка живости для балансировщика / App Platform (без БД и авторизации)."""
    return {"ok": True}
