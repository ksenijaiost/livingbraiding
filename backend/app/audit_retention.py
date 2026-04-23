from __future__ import annotations

"""Удаление старых строк аудита, чтобы таблицы *_audit_logs не росли без ограничений."""

import calendar
import os
from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.time_utils import utcnow_naive
from app.db.models import (
    BookingAuditLog,
    CategoryQuestionnaireFieldAuditLog,
    ClientAuditLog,
    KitAuditLog,
    ProductSaleAuditLog,
    ServiceAuditLog,
    ServiceCategoryAuditLog,
    ServiceQuestionnaireFieldAuditLog,
    ServiceSubcategoryAuditLog,
    Setting,
    SettingAuditLog,
    StudioExpenseAuditLog,
    SubcategoryQuestionnaireFieldAuditLog,
    UserAuditLog,
    VisitAuditLog,
    WorkForInventoryAuditLog,
    WorkRateAuditLog,
)
from app.setting_keys import AUDIT_RETENTION_MONTHS

DEFAULT_AUDIT_RETENTION_MONTHS = 6

_AUDIT_MODELS = (
    UserAuditLog,
    BookingAuditLog,
    VisitAuditLog,
    ClientAuditLog,
    KitAuditLog,
    ProductSaleAuditLog,
    StudioExpenseAuditLog,
    WorkForInventoryAuditLog,
    SettingAuditLog,
    WorkRateAuditLog,
    ServiceCategoryAuditLog,
    ServiceSubcategoryAuditLog,
    ServiceAuditLog,
    CategoryQuestionnaireFieldAuditLog,
    SubcategoryQuestionnaireFieldAuditLog,
    ServiceQuestionnaireFieldAuditLog,
)


def _utc_months_ago(months: int) -> datetime:
    """Несколько календарных месяцев назад в «наивном» UTC (как datetime.utcnow в моделях)."""
    now = utcnow_naive()
    y, m, d = now.year, now.month, now.day
    m -= months
    while m <= 0:
        m += 12
        y -= 1
    last = calendar.monthrange(y, m)[1]
    d = min(d, last)
    return datetime(y, m, d, now.hour, now.minute, now.second, now.microsecond)


def purge_expired_audit_logs(db: Session, *, months: int | None = None) -> int:
    """
    Удаляет записи с changed_at строго раньше порога (сейчас минус N календарных месяцев UTC).

    Переменные окружения (опционально):
    - AUDIT_RETENTION_MONTHS — целое, по умолчанию 6; при 0 или отрицательном — ничего не удаляем.
    - DISABLE_AUDIT_RETENTION — если 1/true/yes — отключить очистку.
    """
    flag = os.environ.get("DISABLE_AUDIT_RETENTION", "").strip().lower()
    if flag in ("1", "true", "yes"):
        return 0

    if months is None:
        env_raw = os.environ.get("AUDIT_RETENTION_MONTHS", "").strip()
        if env_raw:
            try:
                months = int(env_raw)
            except ValueError:
                months = DEFAULT_AUDIT_RETENTION_MONTHS
        else:
            row = db.get(Setting, AUDIT_RETENTION_MONTHS)
            try:
                months = int(str(row.value).strip()) if row and row.value is not None else DEFAULT_AUDIT_RETENTION_MONTHS
            except ValueError:
                months = DEFAULT_AUDIT_RETENTION_MONTHS

    if months <= 0:
        return 0

    cutoff = _utc_months_ago(months)
    total = 0
    for model in _AUDIT_MODELS:
        res = db.execute(delete(model).where(model.changed_at < cutoff))
        rc = res.rowcount
        if rc is not None and rc > 0:
            total += rc
    db.commit()
    return total
