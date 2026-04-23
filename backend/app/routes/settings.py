from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_role
from app.db.models import (
    MaterialPriceCurrent,
    MaterialType,
    Setting,
    SettingAuditLog,
    UserRole,
    WorkRate,
    WorkRateAuditLog,
)
from app.db.session import get_db
from app.display_time import ALLOWED_TIMEZONES, ALLOWED_TIMEZONE_IDS, get_display_timezone
from app.forms_parse import parse_bool, parse_float, parse_int
from app.mix_rates import mix_rates_for_admin_form
from app.audit import diff_fields, write_audit_rows
from app.setting_keys import (
    AUDIT_RETENTION_MONTHS,
    DISPLAY_TIMEZONE,
    EDIT_WINDOW_DAYS,
    KIT_MAX_RESERVES_PER_KIT,
    SALON_CUT_PCT,
)
from app.work_rate_keys import (
    CUSTOM_ORDER_BONUS_MULTIPLIER,
    MIX_KANEK,
    MIX_LENGTH,
    MIX_LIGHT,
    MIX_STANDARD,
    MIX_THERMO,
    STUDIO_SHARE,
    STUDIO_SHARE_OVERRIDE,
)
from app.webui import templates, ctx as _ctx


router = APIRouter()


@router.get("/admin/settings", response_class=HTMLResponse)
def admin_settings_page(
    request: Request,
    saved: int | None = None,
    current_user=Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    salon = db.get(Setting, SALON_CUT_PCT)
    salon_cut_pct = (salon.value if salon and str(salon.value).strip() else "0.5")
    try:
        salon_cut_pct_float = float(str(salon_cut_pct).strip().replace(",", "."))
    except ValueError:
        salon_cut_pct_float = 0.5
    edit_days = db.get(Setting, EDIT_WINDOW_DAYS)
    edit_window_days = (edit_days.value if edit_days and str(edit_days.value).strip() else "2")
    audit_retention_row = db.get(Setting, AUDIT_RETENTION_MONTHS)
    audit_retention_months = (
        audit_retention_row.value if audit_retention_row and str(audit_retention_row.value).strip() else "6"
    )
    pk = db.get(MaterialPriceCurrent, MaterialType.KANEKALON)
    pku = db.get(MaterialPriceCurrent, MaterialType.KUDRI)
    kanek_per_100 = str((pk.price_per_gram * 100) if pk else 400.0)
    kudri_per_100 = str((pku.price_per_gram * 100) if pku else 800.0)
    display_tz = get_display_timezone(db)
    kit_max_row = db.get(Setting, KIT_MAX_RESERVES_PER_KIT)
    kit_max_reserves_per_kit = kit_max_row.value if kit_max_row else "3"

    def _wr_float(key: str, default: float) -> float:
        r = db.scalar(select(WorkRate).where(WorkRate.key == key, WorkRate.is_active.is_(True)))
        if not r:
            return default
        try:
            v = json.loads(r.value_json)
            return float(v)
        except Exception:
            return default

    def _wr_bool(key: str, default: bool) -> bool:
        r = db.scalar(select(WorkRate).where(WorkRate.key == key, WorkRate.is_active.is_(True)))
        if not r:
            return default
        try:
            v = json.loads(r.value_json)
            return bool(v)
        except Exception:
            return default

    studio_share_override = _wr_bool(STUDIO_SHARE_OVERRIDE, False)
    studio_share_effective = (
        _wr_float(STUDIO_SHARE, salon_cut_pct_float) if studio_share_override else salon_cut_pct_float
    )

    work_rates = {
        STUDIO_SHARE: studio_share_effective,
        STUDIO_SHARE_OVERRIDE: studio_share_override,
        **mix_rates_for_admin_form(db),
        CUSTOM_ORDER_BONUS_MULTIPLIER: _wr_float(CUSTOM_ORDER_BONUS_MULTIPLIER, 1.0),
    }

    return templates.TemplateResponse(
        "admin_settings.html",
        _ctx(
            request,
            current_user=current_user,
            salon_cut_pct=salon_cut_pct,
            salon_cut_pct_float=salon_cut_pct_float,
            edit_window_days=edit_window_days,
            audit_retention_months=audit_retention_months,
            kanek_per_100g=kanek_per_100,
            kudri_per_100g=kudri_per_100,
            display_timezone=display_tz,
            kit_max_reserves_per_kit=kit_max_reserves_per_kit,
            timezone_choices=ALLOWED_TIMEZONES,
            saved=bool(saved),
            work_rates=work_rates,
            work_rates_open=False,
            work_rates_saved=False,
            work_rates_error=None,
            payroll_open=False,
        ),
    )


@router.post("/admin/settings")
def admin_settings_save(
    salon_cut_pct: str = Form(...),
    kanek_per_100g: str = Form(...),
    kudri_per_100g: str = Form(...),
    kit_max_reserves_per_kit: str = Form(...),
    current_user=Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    try:
        pct = parse_float(salon_cut_pct, min=0.0, max=1.0, field_name="salon_cut_pct")
        k100 = parse_float(kanek_per_100g, min=0.0, field_name="kanek_per_100g")
        ku100 = parse_float(kudri_per_100g, min=0.0, field_name="kudri_per_100g")
        kmn = parse_int(kit_max_reserves_per_kit, min=1, max=20, field_name="kit_max_reserves_per_kit")
    except ValueError:
        return RedirectResponse(url="/admin/settings?saved=0", status_code=303)

    now = datetime.utcnow()

    row = db.get(Setting, SALON_CUT_PCT)
    before_salon = SimpleNamespace(value=(row.value if row else None))
    if not row:
        row = Setting(key=SALON_CUT_PCT, value=str(pct))
        db.add(row)
    else:
        row.value = str(pct)
    row.updated_at = now
    row.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=SettingAuditLog,
        entity_field="setting_key",
        entity_id=row.key,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before_salon, row, ("value",)),
    )

    for mt, per100 in ((MaterialType.KANEKALON, k100), (MaterialType.KUDRI, ku100)):
        per_g = per100 / 100.0
        mrow = db.get(MaterialPriceCurrent, mt)
        if not mrow:
            db.add(MaterialPriceCurrent(material_type=mt, price_per_gram=per_g, updated_at=now))
        else:
            mrow.price_per_gram = per_g
            mrow.updated_at = now

    kr_row = db.get(Setting, KIT_MAX_RESERVES_PER_KIT)
    before_kr = SimpleNamespace(value=(kr_row.value if kr_row else None))
    if not kr_row:
        kr_row = Setting(key=KIT_MAX_RESERVES_PER_KIT, value=str(kmn))
        db.add(kr_row)
    else:
        kr_row.value = str(kmn)
    kr_row.updated_at = now
    kr_row.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=SettingAuditLog,
        entity_field="setting_key",
        entity_id=kr_row.key,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before_kr, kr_row, ("value",)),
    )

    db.commit()
    return RedirectResponse(url="/admin/settings?saved=1", status_code=303)


@router.post("/admin/settings/system")
def admin_settings_system_save(
    edit_window_days: str = Form(...),
    audit_retention_months: str = Form(...),
    display_timezone: str = Form(...),
    current_user=Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    tz_raw = display_timezone.strip()
    if tz_raw not in ALLOWED_TIMEZONE_IDS:
        return RedirectResponse(url="/admin/settings?saved=0", status_code=303)

    now = datetime.utcnow()

    try:
        days = parse_int(edit_window_days, min=0, max=365, field_name="edit_window_days")
    except ValueError:
        return RedirectResponse(url="/admin/settings?saved=0", status_code=303)
    drow = db.get(Setting, EDIT_WINDOW_DAYS)
    before_days = SimpleNamespace(value=(drow.value if drow else None))
    if not drow:
        drow = Setting(key=EDIT_WINDOW_DAYS, value=str(days))
        db.add(drow)
    else:
        drow.value = str(days)
    drow.updated_at = now
    drow.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=SettingAuditLog,
        entity_field="setting_key",
        entity_id=drow.key,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before_days, drow, ("value",)),
    )

    try:
        months = parse_int(audit_retention_months, min=1, max=36, field_name="audit_retention_months")
    except ValueError:
        return RedirectResponse(url="/admin/settings?saved=0", status_code=303)
    ar_row = db.get(Setting, AUDIT_RETENTION_MONTHS)
    before_ar = SimpleNamespace(value=(ar_row.value if ar_row else None))
    if not ar_row:
        ar_row = Setting(key=AUDIT_RETENTION_MONTHS, value=str(months))
        db.add(ar_row)
    else:
        ar_row.value = str(months)
    ar_row.updated_at = now
    ar_row.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=SettingAuditLog,
        entity_field="setting_key",
        entity_id=ar_row.key,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before_ar, ar_row, ("value",)),
    )

    tz_row = db.get(Setting, DISPLAY_TIMEZONE)
    before_tz = SimpleNamespace(value=(tz_row.value if tz_row else None))
    if not tz_row:
        tz_row = Setting(key=DISPLAY_TIMEZONE, value=tz_raw)
        db.add(tz_row)
    else:
        tz_row.value = tz_raw
    tz_row.updated_at = now
    tz_row.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=SettingAuditLog,
        entity_field="setting_key",
        entity_id=tz_row.key,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before_tz, tz_row, ("value",)),
    )

    db.commit()
    return RedirectResponse(url="/admin/settings?saved=1", status_code=303)


@router.post("/admin/settings/work-rates")
async def admin_settings_work_rates_save(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    form = await request.form()

    try:
        salon = db.get(Setting, SALON_CUT_PCT)
        salon_raw = (salon.value if salon else "0.5")
        try:
            salon_pct = float(str(salon_raw).strip().replace(",", "."))
        except ValueError:
            salon_pct = -1
        if salon_pct < 0 or salon_pct > 1:
            raise ValueError("Процент салона в настройках должен быть в диапазоне 0..1 (для привязки доли студии).")

        studio_share_override = parse_bool(form.get("studio_share_override"))
        if studio_share_override:
            studio_share = parse_float(
                form.get("studio_share"),
                default=float(salon_pct),
                min=0.0,
                max=1.0,
                field_name=STUDIO_SHARE,
            )
        else:
            studio_share = float(salon_pct)

        payload: dict[str, Any] = {
            STUDIO_SHARE: float(studio_share),
            STUDIO_SHARE_OVERRIDE: bool(studio_share_override),
            MIX_LIGHT: parse_float(form.get("mix_light"), default=0.5, min=0.0, field_name=MIX_LIGHT),
            MIX_STANDARD: parse_float(form.get("mix_standard"), default=1.0, min=0.0, field_name=MIX_STANDARD),
            MIX_KANEK: parse_float(form.get("mix_kanek"), default=1.5, min=0.0, field_name=MIX_KANEK),
            MIX_THERMO: parse_float(form.get("mix_thermo"), default=2.0, min=0.0, field_name=MIX_THERMO),
            MIX_LENGTH: parse_float(form.get("mix_length"), default=2.5, min=0.0, field_name=MIX_LENGTH),
            CUSTOM_ORDER_BONUS_MULTIPLIER: parse_float(
                form.get("custom_order_bonus_multiplier"),
                default=1.0,
                min=0.0,
                field_name=CUSTOM_ORDER_BONUS_MULTIPLIER,
            ),
        }
    except ValueError as exc:
        salon = db.get(Setting, SALON_CUT_PCT)
        salon_cut_pct = (salon.value if salon and str(salon.value).strip() else "0.5")
        try:
            salon_cut_pct_float = float(str(salon_cut_pct).strip().replace(",", "."))
        except ValueError:
            salon_cut_pct_float = 0.5
        edit_days = db.get(Setting, EDIT_WINDOW_DAYS)
        edit_window_days = (edit_days.value if edit_days and str(edit_days.value).strip() else "2")
        audit_retention_row = db.get(Setting, AUDIT_RETENTION_MONTHS)
        audit_retention_months = (
            audit_retention_row.value if audit_retention_row and str(audit_retention_row.value).strip() else "6"
        )
        pk = db.get(MaterialPriceCurrent, MaterialType.KANEKALON)
        pku = db.get(MaterialPriceCurrent, MaterialType.KUDRI)
        kanek_per_100 = str((pk.price_per_gram * 100) if pk else 400.0)
        kudri_per_100 = str((pku.price_per_gram * 100) if pku else 800.0)
        display_tz = get_display_timezone(db)
        km_row = db.get(Setting, KIT_MAX_RESERVES_PER_KIT)
        kit_max_reserves_per_kit_val = km_row.value if km_row else "3"

        studio_share_override = parse_bool(form.get("studio_share_override"))
        work_rates = {
            STUDIO_SHARE_OVERRIDE: bool(studio_share_override),
            STUDIO_SHARE: (
                parse_float(form.get("studio_share"), default=salon_cut_pct_float, min=0.0, max=1.0, field_name=STUDIO_SHARE)
                if studio_share_override
                else float(salon_cut_pct_float)
            ),
            MIX_LIGHT: parse_float(form.get("mix_light"), default=0.5, min=0.0, field_name=MIX_LIGHT),
            MIX_STANDARD: parse_float(form.get("mix_standard"), default=1.0, min=0.0, field_name=MIX_STANDARD),
            MIX_KANEK: parse_float(form.get("mix_kanek"), default=1.5, min=0.0, field_name=MIX_KANEK),
            MIX_THERMO: parse_float(form.get("mix_thermo"), default=2.0, min=0.0, field_name=MIX_THERMO),
            MIX_LENGTH: parse_float(form.get("mix_length"), default=2.5, min=0.0, field_name=MIX_LENGTH),
            CUSTOM_ORDER_BONUS_MULTIPLIER: parse_float(
                form.get("custom_order_bonus_multiplier"),
                default=1.0,
                min=0.0,
                field_name=CUSTOM_ORDER_BONUS_MULTIPLIER,
            ),
        }
        return templates.TemplateResponse(
            "admin_settings.html",
            _ctx(
                request,
                current_user=current_user,
                salon_cut_pct=salon_cut_pct,
                salon_cut_pct_float=salon_cut_pct_float,
                edit_window_days=edit_window_days,
                audit_retention_months=audit_retention_months,
                kanek_per_100g=kanek_per_100,
                kudri_per_100g=kudri_per_100,
                display_timezone=display_tz,
                kit_max_reserves_per_kit=kit_max_reserves_per_kit_val,
                timezone_choices=ALLOWED_TIMEZONES,
                saved=False,
                work_rates=work_rates,
                work_rates_open=True,
                work_rates_saved=False,
                work_rates_error=str(exc),
                payroll_open=False,
            ),
            status_code=400,
        )

    now = datetime.utcnow()
    for k, v in payload.items():
        row = db.scalar(select(WorkRate).where(WorkRate.key == k))
        if not row:
            row = WorkRate(
                key=k,
                value_json=json.dumps(v, ensure_ascii=False),
                is_active=True,
                updated_at=now,
                updated_by_user_id=current_user.id,
            )
            db.add(row)
            db.flush()
            before = SimpleNamespace(value_json=None, is_active=None)
        else:
            before = SimpleNamespace(value_json=row.value_json, is_active=row.is_active)
            row.value_json = json.dumps(v, ensure_ascii=False)
            row.is_active = True
            row.updated_at = now
            row.updated_by_user_id = current_user.id
        write_audit_rows(
            db,
            log_model=WorkRateAuditLog,
            entity_field="work_rate_id",
            entity_id=row.id,
            changed_by_user_id=current_user.id,
            changes=diff_fields(before, row, ("value_json", "is_active")),
        )
    db.commit()
    return RedirectResponse(url="/admin/settings?saved=1", status_code=303)

