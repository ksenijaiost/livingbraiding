from __future__ import annotations

"""Shared web UI helpers (Jinja templates and common context).

This module exists to avoid circular imports when splitting `main.py` routes into
separate router modules.
"""

import json
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.consultation_booking import booking_status_display
from app.consultation_types import format_types_display
from app.display_time import format_naive_utc_datetime, timezone_label
from app.planned_service_time import format_planned_service_start_local
from app.ui_service_display import format_duration_minutes_ru
from app.ru_labels import (
    format_price_integer_rub,
    ru_master_level,
    ru_questionnaire_field_type,
    ru_user_role,
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


templates.env.filters["pretty_json"] = _jinja_pretty_json
templates.env.globals["ru_master_level"] = ru_master_level
templates.env.globals["ru_questionnaire_field_type"] = ru_questionnaire_field_type
templates.env.globals["ru_user_role"] = ru_user_role
templates.env.globals["format_price_integer_rub"] = format_price_integer_rub
templates.env.globals["format_naive_utc_datetime"] = format_naive_utc_datetime
templates.env.globals["format_planned_service_start_local"] = format_planned_service_start_local
templates.env.globals["format_duration_minutes_ru"] = format_duration_minutes_ru
templates.env.globals["format_types_display"] = format_types_display
templates.env.globals["timezone_label"] = timezone_label
templates.env.globals["booking_status_display"] = booking_status_display
templates.env.globals["UserRole"] = UserRole
templates.env.globals["master_levels"] = tuple(MasterLevel)


def ctx(request: Request, current_user: Any = None, **kwargs):
    """Common Jinja context: always pass request + current_user."""
    return {"request": request, "current_user": current_user, **kwargs}

