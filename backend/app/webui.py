from __future__ import annotations

"""Shared web UI helpers (Jinja templates and common context).

This module exists to avoid circular imports when splitting `main.py` routes into
separate router modules.
"""

import json
from datetime import date, datetime
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from app.consultation_booking import booking_kind_label, booking_status_display
from app.audit_field_labels import audit_field_label, audit_field_is_json
from app.client_payment import client_payment_kind_label
from app.consultation_types import format_types_display
from app.display_time import (
    DEFAULT_DISPLAY_TIMEZONE,
    format_naive_utc_datetime,
    resolve_request_display_timezone,
    timezone_label,
)
from app.planned_service_time import format_planned_service_start_local
from app.ui_service_display import format_duration_minutes_ru, format_service_catalog_path
from app.ru_labels import (
    format_price_integer_rub,
    ru_master_level,
    ru_questionnaire_field_type,
    ru_user_role,
)
from app.role_access import (
    role_can_edit_catalog,
    role_is_admin_staff,
    role_is_admin_super,
    role_is_master_schedule_admin,
    role_can_delete_kit,
)
from app.db.models import MasterLevel, UserRole

templates = Jinja2Templates(directory="app/templates")


def _jinja_pretty_json(value: str | None) -> str:
    if value is None or str(value).strip() == "":
        return "—"
    try:
        parsed = json.loads(value)
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    except Exception:
        return str(value)


@pass_context
def _jinja_dt_local(context: Any, value: datetime | date | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Показать naive-UTC datetime в display_tz из контекста (настройка студии)."""
    if value is None:
        return "—"
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.strftime(fmt if "%H" not in fmt else "%d.%m.%Y")
    tz = context.get("display_tz") or DEFAULT_DISPLAY_TIMEZONE
    out = format_naive_utc_datetime(value, tz, fmt)
    return out or "—"


templates.env.filters["pretty_json"] = _jinja_pretty_json
templates.env.filters["dt_local"] = _jinja_dt_local
templates.env.globals["ru_master_level"] = ru_master_level
templates.env.globals["ru_questionnaire_field_type"] = ru_questionnaire_field_type
templates.env.globals["ru_user_role"] = ru_user_role
templates.env.globals["format_price_integer_rub"] = format_price_integer_rub
templates.env.globals["format_naive_utc_datetime"] = format_naive_utc_datetime
templates.env.globals["format_planned_service_start_local"] = format_planned_service_start_local
templates.env.globals["format_duration_minutes_ru"] = format_duration_minutes_ru
templates.env.globals["format_service_catalog_path"] = format_service_catalog_path
templates.env.globals["format_types_display"] = format_types_display
templates.env.globals["timezone_label"] = timezone_label
templates.env.globals["booking_status_display"] = booking_status_display
templates.env.globals["audit_field_label"] = audit_field_label
templates.env.globals["audit_field_is_json"] = audit_field_is_json
templates.env.globals["booking_audit_field_label"] = audit_field_label
templates.env.globals["booking_kind_label"] = booking_kind_label
templates.env.globals["client_payment_kind_label"] = client_payment_kind_label
templates.env.globals["UserRole"] = UserRole
templates.env.globals["master_levels"] = tuple(MasterLevel)
templates.env.globals["role_is_admin_staff"] = role_is_admin_staff
templates.env.globals["role_can_edit_catalog"] = role_can_edit_catalog
templates.env.globals["role_is_master_schedule_admin"] = role_is_master_schedule_admin
templates.env.globals["role_can_delete_kit"] = role_can_delete_kit
templates.env.globals["role_is_admin_super"] = role_is_admin_super


def ctx(request: Request, current_user: Any = None, **kwargs):
    """Common Jinja context: always pass request + current_user + display_tz."""
    out = {"request": request, "current_user": current_user, **kwargs}
    if "display_tz" not in out:
        out["display_tz"] = resolve_request_display_timezone(request)
    return out
