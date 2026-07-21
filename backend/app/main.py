from __future__ import annotations

"""
FastAPI entrypoint: создание приложения, подключение роутеров, startup.

Шаблоны Jinja и фильтры/глобалы — в `app/webui.py`.
HTTP-роуты — в `app/routes/`.
"""

import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.access_logging import AccessLogWithUserMiddleware, configure_request_access_logging
from app.admin_questionnaire_fields import router as admin_questionnaire_fields_router
from app.admin_service_catalog import router as admin_service_catalog_router
from app.audit_retention import purge_expired_audit_logs_startup_safe
from app.bootstrap import ensure_initial_techspec_user
from app.db.session import get_db
from app.payroll_fund import backfill_all_visit_master_accruals_if_missing
from app.seed import ensure_dev_seed_data, ensure_prod_seed_data

from app import admin_studio_expenses as admin_studio_expenses_routes
from app import product_sales as product_sales_routes
from app import work_products as work_products_routes

app = FastAPI(title="livingbraiding")
app.add_middleware(AccessLogWithUserMiddleware)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(admin_service_catalog_router)
app.include_router(admin_questionnaire_fields_router)
app.include_router(admin_studio_expenses_routes.router)
app.include_router(product_sales_routes.router)
app.include_router(product_sales_routes.legacy_admin_router)
app.include_router(work_products_routes.router)
app.include_router(work_products_routes.legacy_admin_router)
from app.routes.visits import router as visits_router  # noqa: E402
app.include_router(visits_router)
from app.routes.consultations import router as consultations_router  # noqa: E402
app.include_router(consultations_router)
from app.routes.settings import router as settings_router  # noqa: E402
app.include_router(settings_router)
from app.routes.clients import (  # noqa: E402
    legacy_clients_admin_router,
    router as clients_router,
)
app.include_router(clients_router)
app.include_router(legacy_clients_admin_router)
from app.routes.bookings import (  # noqa: E402
    legacy_bookings_admin_router,
    master_bookings_page_router,
    router as bookings_router,
)
app.include_router(bookings_router)
app.include_router(legacy_bookings_admin_router)
app.include_router(master_bookings_page_router)
from app.routes.kits import (  # noqa: E402
    legacy_kits_admin_router,
    master_kits_router,
    router as kits_router,
)
app.include_router(kits_router)
app.include_router(legacy_kits_admin_router)
app.include_router(master_kits_router)
from app.routes.reports import router as reports_router  # noqa: E402
app.include_router(reports_router)
from app.routes.master_statistics import router as master_statistics_router  # noqa: E402
app.include_router(master_statistics_router)
from app.routes.work_plans import router as work_plans_router  # noqa: E402
app.include_router(work_plans_router)
from app.routes.hourly_work import router as hourly_work_router  # noqa: E402
app.include_router(hourly_work_router)
from app.routes.payroll import router as payroll_router  # noqa: E402
app.include_router(payroll_router)
from app.routes.staff import router as staff_router  # noqa: E402
app.include_router(staff_router)
from app.routes.master_visit import router as master_visit_router  # noqa: E402
app.include_router(master_visit_router)
from app.routes.master_clients import router as master_clients_router  # noqa: E402
app.include_router(master_clients_router)
from app.routes.calendar_api import router as calendar_api_router  # noqa: E402
app.include_router(calendar_api_router)
from app.routes.products_catalog import router as products_catalog_router  # noqa: E402
app.include_router(products_catalog_router)
from app.routes.products_calc import router as products_calc_router  # noqa: E402
app.include_router(products_calc_router)
from app.routes.public_pages import router as public_pages_router  # noqa: E402
app.include_router(public_pages_router)
from app.routes.media import router as media_router  # noqa: E402
app.include_router(media_router)
from app.routes.techspec_media import router as techspec_media_router  # noqa: E402
app.include_router(techspec_media_router)
from app.routes.super_admin_purge import router as super_admin_purge_router  # noqa: E402
app.include_router(super_admin_purge_router)
from app.routes.master_schedule import router as master_schedule_router  # noqa: E402
app.include_router(master_schedule_router)
from app.routes.auth_routes import router as auth_routes_router  # noqa: E402
app.include_router(auth_routes_router)


@app.on_event("startup")
def _startup():
    """Access-лог (время, user) + optional dev seed + audit retention."""
    configure_request_access_logging()
    db = next(get_db())
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
