"""
Суперадмин: управление прайсом (категории / подкатегории / позиции).
В одной модели и услуги для визита, и товары без визита.
Цены: три уровня (младший / мастер / старший), у каждого от и до; пустые поля → NULL в БД.
"""

from __future__ import annotations

import json
from urllib.parse import quote
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_role
from app.audit import diff_fields, write_audit_rows
from app.consultation_types import CONSULTATION_TYPE_CHOICES
from app.db.models import (
    ConsultationKind,
    Service,
    ServiceAuditLog,
    ServiceCategory,
    ServiceCategoryAuditLog,
    ServiceSubcategory,
    ServiceSubcategoryAuditLog,
    UserRole,
    WorkRate,
)
from app.db.session import get_db
from app.price_ordering import price_sort_key, service_sort_price
from app.ru_labels import ru_master_level
from app.time_utils import utcnow_naive
from app.webui import ctx as _ctx
from app.webui import templates
from app.work_rate_keys import PRICE_AUTOCALC_BASE_LEVEL, PRICE_AUTOCALC_DISCOUNTS

router = APIRouter(prefix="/admin/catalog", tags=["admin-catalog"])

_CATALOG_EDITOR = Depends(require_role(UserRole.ADMIN_SENIOR, UserRole.ADMIN_SUPER))
_PRODUCT_CATALOG_ONLY_CATEGORIES = {"Заказ", "Продажа материала", "Работа по фикс цене"}
_PRICE_LEVELS: tuple[str, ...] = ("JUNIOR", "MIDDLE", "SENIOR")
_PRICE_LEVEL_FIELDS: dict[str, tuple[str, str]] = {
    "JUNIOR": ("price_junior_from", "price_junior_to"),
    "MIDDLE": ("price_middle_from", "price_middle_to"),
    "SENIOR": ("price_senior_from", "price_senior_to"),
}
_DEFAULT_AUTOCALC_BASE = "SENIOR"
_DEFAULT_AUTOCALC_DISCOUNTS: dict[str, float] = {"MIDDLE": 20.0, "JUNIOR": 40.0}


def _parse_estimated_duration(raw: object) -> int:
    if raw is None:
        raise ValueError("missing duration")
    s = str(raw).strip()
    if not s:
        raise ValueError("empty duration")
    try:
        v = int(s)
    except ValueError:
        raise ValueError("bad duration")
    if v < 1 or v > 1440:
        raise ValueError("bad duration range")
    return v


def _parse_optional_price(raw: object) -> float | None:
    if raw is None:
        return None
    t = str(raw).strip()
    if not t:
        return None
    return float(t.replace(",", "."))


def _parse_optional_short_name(raw: str | None, *, max_len: int) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    return s[:max_len]


def _parse_autocalc_discount(raw: object | None) -> float | None:
    """Пусто → None (уровень не пересчитываем). Иначе скидка 0..100 от базы."""
    if raw is None:
        return None
    t = str(raw).strip()
    if not t:
        return None
    v = float(t.replace(",", "."))
    if v < 0 or v > 100:
        raise ValueError("discount range")
    return float(v)


def _round_rubles(v: float) -> float:
    if v >= 0:
        return float(int(v + 0.5))
    return float(int(v - 0.5))


def _autocalc_discounted_price(base: float | None, *, discount_pct: float) -> float | None:
    if base is None:
        return None
    return _round_rubles(float(base) * (100.0 - float(discount_pct)) / 100.0)


def _load_autocalc_prefs(db: Session) -> tuple[str, dict[str, float]]:
    base = _DEFAULT_AUTOCALC_BASE
    discounts = dict(_DEFAULT_AUTOCALC_DISCOUNTS)
    base_row = db.scalar(select(WorkRate).where(WorkRate.key == PRICE_AUTOCALC_BASE_LEVEL))
    if base_row and (base_row.value_json or "").strip():
        try:
            raw = json.loads(base_row.value_json)
            code = str(raw or "").strip().upper()
            if code in _PRICE_LEVEL_FIELDS:
                base = code
        except (TypeError, ValueError):
            pass
    disc_row = db.scalar(select(WorkRate).where(WorkRate.key == PRICE_AUTOCALC_DISCOUNTS))
    if disc_row and (disc_row.value_json or "").strip():
        try:
            raw = json.loads(disc_row.value_json)
        except (TypeError, ValueError):
            raw = None
        if isinstance(raw, dict):
            parsed: dict[str, float] = {}
            for level, val in raw.items():
                code = str(level or "").strip().upper()
                if code not in _PRICE_LEVEL_FIELDS or code == base:
                    continue
                try:
                    d = float(val)
                except (TypeError, ValueError):
                    continue
                if 0 <= d <= 100:
                    parsed[code] = d
            discounts = parsed
    return base, discounts


def _upsert_work_rate_json(db: Session, *, key: str, value: object, user_id: int) -> None:
    payload = json.dumps(value, ensure_ascii=False)
    now = utcnow_naive()
    row = db.scalar(select(WorkRate).where(WorkRate.key == key))
    if row is None:
        db.add(
            WorkRate(
                key=key,
                value_json=payload,
                is_active=True,
                updated_at=now,
                updated_by_user_id=user_id,
            )
        )
        return
    row.value_json = payload
    row.is_active = True
    row.updated_at = now
    row.updated_by_user_id = user_id


def _save_autocalc_prefs(
    db: Session,
    *,
    base_level: str,
    discounts: dict[str, float],
    user_id: int,
) -> None:
    clean = {
        code: float(val)
        for code, val in discounts.items()
        if code in _PRICE_LEVEL_FIELDS and code != base_level and 0 <= float(val) <= 100
    }
    _upsert_work_rate_json(db, key=PRICE_AUTOCALC_BASE_LEVEL, value=base_level, user_id=user_id)
    _upsert_work_rate_json(db, key=PRICE_AUTOCALC_DISCOUNTS, value=clean, user_id=user_id)


def _autocalc_done_message(
    *,
    selected: int,
    updated: int,
    base_level: str,
    discounts: dict[str, float],
) -> str:
    bits: list[str] = []
    for code in _PRICE_LEVELS:
        if code not in discounts:
            continue
        d = discounts[code]
        shown = str(int(d)) if float(d) == int(d) else str(d).rstrip("0").rstrip(".")
        bits.append(f"{ru_master_level(code)} −{shown}%")
    tail = ", ".join(bits) if bits else "без скидок"
    return (
        f"Готово: выбрано {int(selected)}, обновлено {int(updated)}. "
        f"От {ru_master_level(base_level)}: {tail}."
    )


def _allowed_service_scope_rows(db: Session) -> list[tuple[ServiceCategory, ServiceSubcategory, Service]]:
    return list(
        db.execute(
            select(ServiceCategory, ServiceSubcategory, Service)
            .join(ServiceSubcategory, ServiceSubcategory.category_id == ServiceCategory.id)
            .join(Service, Service.subcategory_id == ServiceSubcategory.id)
            .where(ServiceCategory.name.not_in(_PRODUCT_CATALOG_ONLY_CATEGORIES))
            .order_by(ServiceCategory.name.asc(), ServiceSubcategory.name.asc(), Service.id.asc())
        ).all()
    )


def _autocalc_scope_options(db: Session) -> dict[str, list[dict[str, Any]]]:
    rows = _allowed_service_scope_rows(db)
    categories: dict[int, dict[str, Any]] = {}
    subcategories: dict[int, dict[str, Any]] = {}
    services: list[dict[str, Any]] = []
    for cat, sub, svc in rows:
        categories[int(cat.id)] = {"id": int(cat.id), "name": cat.name}
        subcategories[int(sub.id)] = {"id": int(sub.id), "name": sub.name, "category_id": int(cat.id)}
        services.append(
            {
                "id": int(svc.id),
                "name": svc.name,
                "is_active": bool(svc.is_active),
                "category_id": int(cat.id),
                "category_name": cat.name,
                "subcategory_id": int(sub.id),
                "subcategory_name": sub.name,
            }
        )
    return {
        "categories": sorted(categories.values(), key=lambda x: str(x["name"]).lower()),
        "subcategories": sorted(subcategories.values(), key=lambda x: (x["category_id"], str(x["name"]).lower())),
        "services": services,
    }


def _resolve_autocalc_service_ids(
    db: Session,
    *,
    scope_mode: str,
    category_id: int | None,
    subcategory_id: int | None,
    service_ids: list[int],
) -> list[int]:
    rows = _allowed_service_scope_rows(db)
    allowed_cat_ids = {int(cat.id) for cat, _sub, _svc in rows}
    allowed_sub_ids = {int(sub.id) for _cat, sub, _svc in rows}
    allowed_service_ids = {int(svc.id) for _cat, _sub, svc in rows}

    if scope_mode == "all":
        return sorted(allowed_service_ids)
    if scope_mode == "category":
        if category_id is None or int(category_id) not in allowed_cat_ids:
            raise ValueError("Выберите категорию.")
        return sorted({int(svc.id) for cat, _sub, svc in rows if int(cat.id) == int(category_id)})
    if scope_mode == "subcategory":
        if category_id is None or int(category_id) not in allowed_cat_ids:
            raise ValueError("Выберите категорию.")
        if subcategory_id is None or int(subcategory_id) not in allowed_sub_ids:
            raise ValueError("Выберите подкатегорию.")
        return sorted(
            {
                int(svc.id)
                for cat, sub, svc in rows
                if int(cat.id) == int(category_id) and int(sub.id) == int(subcategory_id)
            }
        )
    if scope_mode == "services":
        clean_ids = sorted({int(i) for i in service_ids if int(i) > 0})
        if not clean_ids:
            raise ValueError("Выберите хотя бы одну услугу.")
        bad = [i for i in clean_ids if i not in allowed_service_ids]
        if bad:
            raise ValueError("Выбраны недопустимые услуги.")
        return clean_ids
    raise ValueError("Некорректный режим выбора позиций.")


def _autocalc_apply_for_services(
    db: Session,
    *,
    services: list[Service],
    base_level: str,
    discounts: dict[str, float],
    changed_by_user_id: int,
) -> int:
    base_from, base_to = _PRICE_LEVEL_FIELDS[base_level]
    targets = {
        code: float(val)
        for code, val in discounts.items()
        if code in _PRICE_LEVEL_FIELDS and code != base_level
    }
    updated_count = 0
    for svc in services:
        before = SimpleNamespace(
            price_junior_from=svc.price_junior_from,
            price_junior_to=svc.price_junior_to,
            price_middle_from=svc.price_middle_from,
            price_middle_to=svc.price_middle_to,
            price_senior_from=svc.price_senior_from,
            price_senior_to=svc.price_senior_to,
        )
        changed = False
        src_from_val = getattr(svc, base_from)
        src_to_val = getattr(svc, base_to)
        for level, discount_pct in targets.items():
            target_from, target_to = _PRICE_LEVEL_FIELDS[level]
            new_from = _autocalc_discounted_price(src_from_val, discount_pct=discount_pct)
            new_to = _autocalc_discounted_price(src_to_val, discount_pct=discount_pct)
            if new_from is not None and getattr(svc, target_from) != new_from:
                setattr(svc, target_from, new_from)
                changed = True
            if new_to is not None and getattr(svc, target_to) != new_to:
                setattr(svc, target_to, new_to)
                changed = True
        if not changed:
            continue
        svc.updated_at = utcnow_naive()
        svc.updated_by_user_id = changed_by_user_id
        write_audit_rows(
            db,
            log_model=ServiceAuditLog,
            entity_field="service_id",
            entity_id=svc.id,
            changed_by_user_id=changed_by_user_id,
            changes=diff_fields(
                before,
                svc,
                (
                    "price_junior_from",
                    "price_junior_to",
                    "price_middle_from",
                    "price_middle_to",
                    "price_senior_from",
                    "price_senior_to",
                ),
            ),
        )
        updated_count += 1
    return updated_count


def _fmt_discount_input(v: float | None) -> str:
    if v is None:
        return ""
    if float(v) == int(v):
        return str(int(v))
    return str(v).rstrip("0").rstrip(".")


def _render_price_autocalc_page(
    request: Request,
    *,
    current_user: AuthUser,
    db: Session,
    error: str | None = None,
    msg: str | None = None,
    selected_count: int | None = None,
    updated_count: int | None = None,
    done_message: str | None = None,
    fp: dict[str, Any] | None = None,
    status_code: int = 200,
):
    options = _autocalc_scope_options(db)
    saved_base, saved_discounts = _load_autocalc_prefs(db)
    data = fp or {}
    base_level = str(data.get("base_level") or saved_base or _DEFAULT_AUTOCALC_BASE).strip().upper()
    if base_level not in _PRICE_LEVEL_FIELDS:
        base_level = _DEFAULT_AUTOCALC_BASE
    discount_inputs: dict[str, str] = {}
    for code in _PRICE_LEVELS:
        key = f"discount_{code}"
        if key in data:
            discount_inputs[code] = str(data.get(key) or "")
        elif code == base_level:
            discount_inputs[code] = ""
        elif code in saved_discounts:
            discount_inputs[code] = _fmt_discount_input(saved_discounts.get(code))
        else:
            discount_inputs[code] = ""
    level_labels = {code: ru_master_level(code) for code in _PRICE_LEVELS}
    return templates.TemplateResponse(
        "admin_catalog_price_autocalc.html",
        _ctx(
            request,
            current_user,
            error=error,
            msg=msg,
            selected_count=selected_count,
            updated_count=updated_count,
            done_message=done_message,
            scope_options=options,
            level_labels=level_labels,
            price_levels=list(_PRICE_LEVELS),
            fp={
                "scope_mode": data.get("scope_mode") or "all",
                "category_id": str(data.get("category_id") or ""),
                "subcategory_id": str(data.get("subcategory_id") or ""),
                "service_ids": [int(x) for x in (data.get("service_ids") or []) if int(x) > 0],
                "base_level": base_level,
                "discounts": discount_inputs,
            },
        ),
        status_code=status_code,
    )


def _fmt_price_input(v: float | None) -> str:
    if v is None:
        return ""
    return str(v).replace(".", ",")


def _is_checked(raw: object | None) -> bool:
    if raw is None:
        return False
    return str(raw).lower() in ("1", "on", "true", "yes")


def _parse_tri_state(raw: object | None) -> bool | None:
    """
    Радио с 3 значениями:
    - "" / None / "inherit" → None (как у подкатегории)
    - "show" → True
    - "hide" → False
    """
    if raw is None:
        return None
    t = str(raw).strip().lower()
    if t in ("", "inherit", "none", "null"):
        return None
    if t in ("show", "1", "on", "true", "yes"):
        return True
    if t in ("hide", "0", "off", "false", "no"):
        return False
    return None


def _parse_kit_section_override(raw: object | None) -> bool | None:
    """Пусто → наследовать от подкатегории (NULL в БД)."""
    if raw is None:
        return None
    t = str(raw).strip()
    if not t:
        return None
    low = t.lower()
    if low in ("1", "on", "true", "yes"):
        return True
    if low in ("0", "off", "false", "no"):
        return False
    return None


def _parse_tail_section_override(raw: object | None) -> bool | None:
    """Пусто → наследовать от подкатегории (NULL в БД)."""
    return _parse_kit_section_override(raw)


@router.get("", response_class=HTMLResponse)
def catalog_index(
    request: Request,
    err: str | None = None,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    raw = db.execute(
        select(
            ServiceCategory,
            func.count(ServiceSubcategory.id).label("sub_count"),
        )
        .outerjoin(ServiceSubcategory, ServiceSubcategory.category_id == ServiceCategory.id)
        .group_by(ServiceCategory.id)
        .order_by(ServiceCategory.id)
    ).all()
    svc_by_cat: dict[int, int] = {
        int(cat_id): int(cnt)
        for cat_id, cnt in db.execute(
            select(ServiceSubcategory.category_id, func.count(Service.id))
            .join(Service, Service.subcategory_id == ServiceSubcategory.id)
            .group_by(ServiceSubcategory.category_id)
        ).all()
    }
    cat_rows = [
        {
            "category": c,
            "sub_count": int(n),
            "service_count": svc_by_cat.get(c.id, 0),
        }
        for c, n in raw
        if (c.name or "").strip() not in _PRODUCT_CATALOG_ONLY_CATEGORIES
    ]
    total_services = sum(r["service_count"] for r in cat_rows)
    return templates.TemplateResponse(
        "admin_catalog_index.html",
        _ctx(
            request,
            current_user,
            cat_rows=cat_rows,
            total_services=total_services,
            err=err,
        ),
    )


@router.get("/price-autocalc", response_class=HTMLResponse)
def catalog_price_autocalc_form(
    request: Request,
    msg: str | None = None,
    selected: int | None = Query(None),
    updated: int | None = Query(None),
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    done_message = None
    if msg == "done":
        base_level, discounts = _load_autocalc_prefs(db)
        done_message = _autocalc_done_message(
            selected=int(selected or 0),
            updated=int(updated or 0),
            base_level=base_level,
            discounts=discounts,
        )
    return _render_price_autocalc_page(
        request,
        current_user=current_user,
        db=db,
        msg=msg,
        selected_count=selected,
        updated_count=updated,
        done_message=done_message,
    )


@router.post("/price-autocalc", response_class=HTMLResponse)
async def catalog_price_autocalc_apply(
    request: Request,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    form = await request.form()
    scope_mode = str(form.get("scope_mode") or "all").strip().lower()
    category_raw = str(form.get("category_id") or "").strip()
    subcategory_raw = str(form.get("subcategory_id") or "").strip()
    service_ids_raw = form.getlist("service_ids")
    base_level = str(form.get("base_level") or "").strip().upper()
    fp: dict[str, Any] = {
        "scope_mode": scope_mode,
        "category_id": category_raw,
        "subcategory_id": subcategory_raw,
        "service_ids": [int(x) for x in service_ids_raw if str(x).strip().isdigit()],
        "base_level": base_level,
    }
    for code in _PRICE_LEVELS:
        fp[f"discount_{code}"] = str(form.get(f"discount_{code}") or "")

    if base_level not in _PRICE_LEVEL_FIELDS:
        return _render_price_autocalc_page(
            request,
            current_user=current_user,
            db=db,
            error="Выберите базовый уровень.",
            fp=fp,
            status_code=400,
        )

    discounts: dict[str, float] = {}
    try:
        for code in _PRICE_LEVELS:
            if code == base_level:
                continue
            parsed = _parse_autocalc_discount(form.get(f"discount_{code}"))
            if parsed is not None:
                discounts[code] = parsed
    except ValueError:
        return _render_price_autocalc_page(
            request,
            current_user=current_user,
            db=db,
            error="Скидка: число от 0 до 100. Пустое поле — уровень не пересчитывается.",
            fp=fp,
            status_code=400,
        )
    if not discounts:
        return _render_price_autocalc_page(
            request,
            current_user=current_user,
            db=db,
            error="Укажите скидку хотя бы для одного уровня.",
            fp=fp,
            status_code=400,
        )

    category_id = int(category_raw) if category_raw.isdigit() else None
    subcategory_id = int(subcategory_raw) if subcategory_raw.isdigit() else None
    service_ids = [int(x) for x in service_ids_raw if str(x).strip().isdigit()]
    try:
        resolved_service_ids = _resolve_autocalc_service_ids(
            db,
            scope_mode=scope_mode,
            category_id=category_id,
            subcategory_id=subcategory_id,
            service_ids=service_ids,
        )
    except ValueError as exc:
        return _render_price_autocalc_page(
            request,
            current_user=current_user,
            db=db,
            error=str(exc),
            fp=fp,
            status_code=400,
        )
    if not resolved_service_ids:
        return _render_price_autocalc_page(
            request,
            current_user=current_user,
            db=db,
            error="Нет услуг для выбранной области.",
            fp=fp,
            status_code=400,
        )
    services = list(
        db.scalars(select(Service).where(Service.id.in_(resolved_service_ids)).order_by(Service.id.asc())).all()
    )
    updated_count = _autocalc_apply_for_services(
        db,
        services=services,
        base_level=base_level,
        discounts=discounts,
        changed_by_user_id=current_user.id,
    )
    _save_autocalc_prefs(
        db,
        base_level=base_level,
        discounts=discounts,
        user_id=int(current_user.id),
    )
    db.commit()
    return RedirectResponse(
        url=f"/admin/catalog/price-autocalc?msg=done&selected={len(resolved_service_ids)}&updated={updated_count}",
        status_code=303,
    )


@router.get("/categories/new", response_class=HTMLResponse)
def category_new_form(
    request: Request,
    err: str | None = None,
    current_user: AuthUser = _CATALOG_EDITOR,
):
    return templates.TemplateResponse(
        "admin_catalog_category_form.html",
        _ctx(
            request,
            current_user,
            err=err,
            is_new=True,
            category=None,
            form_name="",
            form_short_name="",
            form_active=True,
            form_include_in_visit=True,
            form_consultation_kind=ConsultationKind.BRAIDING.value,
            consultation_kind_choices=CONSULTATION_TYPE_CHOICES,
        ),
    )


def _parse_consultation_kind(raw: str | None) -> ConsultationKind:
    v = (raw or "").strip().upper()
    try:
        return ConsultationKind(v)
    except ValueError:
        return ConsultationKind.BRAIDING


@router.post("/categories/new")
def category_new_save(
    name: str = Form(...),
    short_name: str | None = Form(None),
    is_active: str | None = Form(None),
    consultation_kind: str = Form(ConsultationKind.BRAIDING.value),
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    nm = (name or "").strip()
    if not nm:
        return RedirectResponse(url="/admin/catalog/categories/new?err=empty", status_code=303)
    cat = ServiceCategory(
        name=nm,
        short_name=_parse_optional_short_name(short_name, max_len=120),
        is_active=_is_checked(is_active),
        consultation_kind=_parse_consultation_kind(consultation_kind),
    )
    db.add(cat)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(url="/admin/catalog/categories/new?err=duplicate", status_code=303)
    return RedirectResponse(url=f"/admin/catalog/categories/{cat.id}/subcategories", status_code=303)


@router.get("/categories/{category_id}/edit", response_class=HTMLResponse)
def category_edit_form(
    request: Request,
    category_id: int,
    err: str | None = None,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    cat = db.get(ServiceCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    from app.care_memos import memo_form_context

    return templates.TemplateResponse(
        "admin_catalog_category_form.html",
        _ctx(
            request,
            current_user,
            err=err,
            is_new=False,
            category=cat,
            form_name=cat.name,
            form_short_name=cat.short_name or "",
            form_active=cat.is_active,
            form_include_in_visit=True,
            form_consultation_kind=cat.consultation_kind.value,
            consultation_kind_choices=CONSULTATION_TYPE_CHOICES,
            **memo_form_context(db, cat),
        ),
    )


@router.post("/categories/{category_id}/edit")
def category_edit_save(
    category_id: int,
    name: str = Form(...),
    short_name: str | None = Form(None),
    is_active: str | None = Form(None),
    consultation_kind: str = Form(ConsultationKind.BRAIDING.value),
    care_memo_before: str | None = Form(None),
    care_memo_after: str | None = Form(None),
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    cat = db.get(ServiceCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    before = SimpleNamespace(
        name=cat.name,
        short_name=cat.short_name,
        is_active=cat.is_active,
        consultation_kind=cat.consultation_kind,
    )
    nm = (name or "").strip()
    if not nm:
        return RedirectResponse(
            url=f"/admin/catalog/categories/{category_id}/edit?err=empty",
            status_code=303,
        )
    cat.name = nm
    cat.short_name = _parse_optional_short_name(short_name, max_len=120)
    cat.is_active = _is_checked(is_active)
    cat.consultation_kind = _parse_consultation_kind(consultation_kind)
    from app.care_memos import apply_category_memos

    try:
        apply_category_memos(db, cat, care_memo_before, care_memo_after)
    except ValueError:
        return RedirectResponse(
            url=f"/admin/catalog/categories/{category_id}/edit?err=memo",
            status_code=303,
        )
    cat.updated_at = utcnow_naive()
    cat.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=ServiceCategoryAuditLog,
        entity_field="category_id",
        entity_id=cat.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before, cat, ("name", "short_name", "is_active", "consultation_kind")),
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url=f"/admin/catalog/categories/{category_id}/edit?err=duplicate",
            status_code=303,
        )
    return RedirectResponse(url=f"/admin/catalog/categories/{category_id}/subcategories", status_code=303)


@router.get("/categories/{category_id}/subcategories", response_class=HTMLResponse)
def subcategory_list(
    request: Request,
    category_id: int,
    err: str | None = None,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    cat = db.get(ServiceCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    if (cat.name or "").strip() in _PRODUCT_CATALOG_ONLY_CATEGORIES:
        return RedirectResponse(url="/products-catalog?category=" + quote(cat.name, safe=""), status_code=303)
    subs = (
        db.scalars(
            select(ServiceSubcategory)
            .where(ServiceSubcategory.category_id == category_id)
            .order_by(ServiceSubcategory.id)
        )
        .all()
    )
    svc_by_sub: dict[int, int] = {
        int(sub_id): int(cnt)
        for sub_id, cnt in db.execute(
            select(Service.subcategory_id, func.count(Service.id))
            .join(ServiceSubcategory, Service.subcategory_id == ServiceSubcategory.id)
            .where(ServiceSubcategory.category_id == category_id)
            .group_by(Service.subcategory_id)
        ).all()
    }
    sub_rows = [
        {"sub": s, "service_count": svc_by_sub.get(s.id, 0)} for s in subs
    ]
    total_services = sum(r["service_count"] for r in sub_rows)
    return templates.TemplateResponse(
        "admin_catalog_subcategories.html",
        _ctx(
            request,
            current_user,
            category=cat,
            sub_rows=sub_rows,
            total_services=total_services,
            err=err,
        ),
    )


@router.get("/subcategories/new", response_class=HTMLResponse)
def subcategory_new_form(
    request: Request,
    category_id: int = Query(...),
    err: str | None = None,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    cat = db.get(ServiceCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    if (cat.name or "").strip() in _PRODUCT_CATALOG_ONLY_CATEGORIES:
        return RedirectResponse(url="/products-catalog?category=" + quote(cat.name, safe=""), status_code=303)
    return templates.TemplateResponse(
        "admin_catalog_subcategory_form.html",
        _ctx(
            request,
            current_user,
            err=err,
            is_new=True,
            category=cat,
            sub=None,
            form_name="",
            form_short_name="",
            form_active=True,
            form_show_kit=False,
            form_show_tail=False,
            form_show_material=True,
            form_show_thermo=False,
        ),
    )


@router.post("/subcategories/new")
def subcategory_new_save(
    category_id: int = Form(...),
    name: str = Form(...),
    short_name: str | None = Form(None),
    is_active: str | None = Form(None),
    show_kit_section: str | None = Form(None),
    show_tail_section: str | None = Form(None),
    show_material_description: str | None = Form(None),
    show_thermo_visit: str | None = Form(None),
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    cat = db.get(ServiceCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    if (cat.name or "").strip() in _PRODUCT_CATALOG_ONLY_CATEGORIES:
        return RedirectResponse(url="/products-catalog?category=" + quote(cat.name, safe=""), status_code=303)
    nm = (name or "").strip()
    if not nm:
        q = quote(str(category_id))
        return RedirectResponse(url=f"/admin/catalog/subcategories/new?category_id={q}&err=empty", status_code=303)
    sub = ServiceSubcategory(
        category_id=category_id,
        name=nm,
        short_name=_parse_optional_short_name(short_name, max_len=160),
        is_active=_is_checked(is_active),
        show_kit_section=_is_checked(show_kit_section),
        show_tail_section=_is_checked(show_tail_section),
        show_material_description=_is_checked(show_material_description),
        show_thermo_visit=_is_checked(show_thermo_visit),
    )
    db.add(sub)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        q = quote(str(category_id))
        return RedirectResponse(url=f"/admin/catalog/subcategories/new?category_id={q}&err=duplicate", status_code=303)
    return RedirectResponse(url=f"/admin/catalog/subcategories/{sub.id}/services", status_code=303)


@router.get("/subcategories/{subcategory_id}/edit", response_class=HTMLResponse)
def subcategory_edit_form(
    request: Request,
    subcategory_id: int,
    err: str | None = None,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    sub = db.get(ServiceSubcategory, subcategory_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Подкатегория не найдена")
    cat = db.get(ServiceCategory, sub.category_id)
    if cat and (cat.name or "").strip() in _PRODUCT_CATALOG_ONLY_CATEGORIES:
        return RedirectResponse(url="/products-catalog?category=" + quote(cat.name, safe=""), status_code=303)
    from app.care_memos import memo_form_context

    return templates.TemplateResponse(
        "admin_catalog_subcategory_form.html",
        _ctx(
            request,
            current_user,
            err=err,
            is_new=False,
            category=cat,
            sub=sub,
            form_name=sub.name,
            form_short_name=sub.short_name or "",
            form_active=sub.is_active,
            form_show_kit=sub.show_kit_section,
            form_show_tail=getattr(sub, "show_tail_section", False),
            form_show_material=sub.show_material_description,
            form_show_thermo=sub.show_thermo_visit,
            **memo_form_context(db, sub),
        ),
    )


@router.post("/subcategories/{subcategory_id}/edit")
def subcategory_edit_save(
    subcategory_id: int,
    name: str = Form(...),
    short_name: str | None = Form(None),
    is_active: str | None = Form(None),
    show_kit_section: str | None = Form(None),
    show_tail_section: str | None = Form(None),
    show_material_description: str | None = Form(None),
    show_thermo_visit: str | None = Form(None),
    care_memo_before: str | None = Form(None),
    care_memo_after: str | None = Form(None),
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    sub = db.get(ServiceSubcategory, subcategory_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Подкатегория не найдена")
    before = SimpleNamespace(
        name=sub.name,
        short_name=sub.short_name,
        is_active=sub.is_active,
        show_kit_section=sub.show_kit_section,
        show_tail_section=getattr(sub, "show_tail_section", False),
        show_material_description=sub.show_material_description,
        show_thermo_visit=sub.show_thermo_visit,
    )
    nm = (name or "").strip()
    cat = db.get(ServiceCategory, sub.category_id)
    if cat and (cat.name or "").strip() in _PRODUCT_CATALOG_ONLY_CATEGORIES:
        return RedirectResponse(url="/products-catalog?category=" + quote(cat.name, safe=""), status_code=303)
    if not nm:
        return RedirectResponse(
            url=f"/admin/catalog/subcategories/{subcategory_id}/edit?err=empty",
            status_code=303,
        )
    sub.name = nm
    sub.short_name = _parse_optional_short_name(short_name, max_len=160)
    sub.is_active = _is_checked(is_active)
    sub.show_kit_section = _is_checked(show_kit_section)
    sub.show_tail_section = _is_checked(show_tail_section)
    sub.show_material_description = _is_checked(show_material_description)
    sub.show_thermo_visit = _is_checked(show_thermo_visit)
    from app.care_memos import apply_entity_memos

    try:
        apply_entity_memos(db, sub, care_memo_before, care_memo_after)
    except ValueError:
        return RedirectResponse(
            url=f"/admin/catalog/subcategories/{subcategory_id}/edit?err=memo",
            status_code=303,
        )
    sub.updated_at = utcnow_naive()
    sub.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=ServiceSubcategoryAuditLog,
        entity_field="subcategory_id",
        entity_id=sub.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(
            before,
            sub,
            (
                "name",
                "short_name",
                "is_active",
                "show_kit_section",
                "show_tail_section",
                "show_material_description",
                "show_thermo_visit",
            ),
        ),
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url=f"/admin/catalog/subcategories/{subcategory_id}/edit?err=duplicate",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/admin/catalog/categories/{sub.category_id}/subcategories",
        status_code=303,
    )


@router.get("/subcategories/{subcategory_id}/services", response_class=HTMLResponse)
def service_list(
    request: Request,
    subcategory_id: int,
    err: str | None = None,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    sub = db.get(ServiceSubcategory, subcategory_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Подкатегория не найдена")
    cat = db.get(ServiceCategory, sub.category_id)
    if cat and (cat.name or "").strip() in _PRODUCT_CATALOG_ONLY_CATEGORIES:
        return RedirectResponse(url="/products-catalog?category=" + quote(cat.name, safe=""), status_code=303)
    services = sorted(
        list(
            db.scalars(
                select(Service).where(Service.subcategory_id == subcategory_id)
            ).all()
        ),
        key=lambda s: (
            not bool(s.is_active),
            *price_sort_key(service_sort_price(s), name=s.name, secondary=(int(s.id),)),
        ),
    )
    return templates.TemplateResponse(
        "admin_catalog_services.html",
        _ctx(request, current_user, category=cat, subcategory=sub, services=services, err=err),
    )


@router.get("/services/new", response_class=HTMLResponse)
def service_new_form(
    request: Request,
    subcategory_id: int = Query(...),
    err: str | None = None,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    sub = db.get(ServiceSubcategory, subcategory_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Подкатегория не найдена")
    cat = db.get(ServiceCategory, sub.category_id)
    if cat and (cat.name or "").strip() in _PRODUCT_CATALOG_ONLY_CATEGORIES:
        return RedirectResponse(url="/products-catalog?category=" + quote(cat.name, safe=""), status_code=303)
    return templates.TemplateResponse(
        "admin_catalog_service_form.html",
        _ctx(
            request,
            current_user,
            err=err,
            is_new=True,
            category=cat,
            subcategory=sub,
            svc=None,
            form_name="",
            form_short_name="",
            form_active=True,
            form_jf="",
            form_jt="",
            form_mf="",
            form_mt="",
            form_sf="",
            form_st="",
            form_kit_override="",
            form_tail_override="",
            form_material_desc_override="",
            form_retail_kanekalon=False,
            form_retail_kudri=False,
            form_retail_mix=False,
            form_duration="120",
        ),
    )


@router.post("/services/new")
def service_new_save(
    subcategory_id: int = Form(...),
    name: str = Form(...),
    short_name: str | None = Form(None),
    is_active: str | None = Form(None),
    price_junior_from: str | None = Form(None),
    price_junior_to: str | None = Form(None),
    price_middle_from: str | None = Form(None),
    price_middle_to: str | None = Form(None),
    price_senior_from: str | None = Form(None),
    price_senior_to: str | None = Form(None),
    kit_section_override: str | None = Form(None),
    tail_section_override: str | None = Form(None),
    material_description_override: str | None = Form(None),
    retail_material_kanekalon: str | None = Form(None),
    retail_material_kudri: str | None = Form(None),
    retail_material_mix: str | None = Form(None),
    estimated_duration_minutes: str = Form(...),
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    sub = db.get(ServiceSubcategory, subcategory_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Подкатегория не найдена")
    cat = db.get(ServiceCategory, sub.category_id)
    if cat and (cat.name or "").strip() in _PRODUCT_CATALOG_ONLY_CATEGORIES:
        return RedirectResponse(url="/products-catalog?category=" + quote(cat.name, safe=""), status_code=303)
    nm = (name or "").strip()
    if not nm:
        q = quote(str(subcategory_id))
        return RedirectResponse(url=f"/admin/catalog/services/new?subcategory_id={q}&err=empty", status_code=303)
    try:
        duration = _parse_estimated_duration(estimated_duration_minutes)
    except ValueError:
        q = quote(str(subcategory_id))
        return RedirectResponse(url=f"/admin/catalog/services/new?subcategory_id={q}&err=bad_duration", status_code=303)
    try:
        jf = _parse_optional_price(price_junior_from)
        jt = _parse_optional_price(price_junior_to)
        mf = _parse_optional_price(price_middle_from)
        mt = _parse_optional_price(price_middle_to)
        sf = _parse_optional_price(price_senior_from)
        st = _parse_optional_price(price_senior_to)
    except ValueError:
        q = quote(str(subcategory_id))
        return RedirectResponse(url=f"/admin/catalog/services/new?subcategory_id={q}&err=bad_price", status_code=303)

    svc = Service(
        subcategory_id=subcategory_id,
        name=nm,
        short_name=_parse_optional_short_name(short_name, max_len=200),
        is_active=_is_checked(is_active),
        estimated_duration_minutes=duration,
        price_junior_from=jf,
        price_junior_to=jt,
        price_middle_from=mf,
        price_middle_to=mt,
        price_senior_from=sf,
        price_senior_to=st,
        kit_section_override=_parse_kit_section_override(kit_section_override),
        tail_section_override=_parse_tail_section_override(tail_section_override),
        material_description_override=_parse_tri_state(material_description_override),
        retail_material_kanekalon=_is_checked(retail_material_kanekalon),
        retail_material_kudri=_is_checked(retail_material_kudri),
        retail_material_mix=_is_checked(retail_material_mix),
    )
    db.add(svc)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        q = quote(str(subcategory_id))
        return RedirectResponse(
            url=f"/admin/catalog/services/new?subcategory_id={q}&err=duplicate",
            status_code=303,
        )
    return RedirectResponse(url=f"/admin/catalog/subcategories/{subcategory_id}/services", status_code=303)


@router.get("/services/{service_id}/edit", response_class=HTMLResponse)
def service_edit_form(
    request: Request,
    service_id: int,
    err: str | None = None,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    svc = db.get(Service, service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail="Услуга не найдена")
    sub = db.get(ServiceSubcategory, svc.subcategory_id)
    cat = db.get(ServiceCategory, sub.category_id) if sub else None
    if cat and (cat.name or "").strip() in _PRODUCT_CATALOG_ONLY_CATEGORIES:
        return RedirectResponse(url="/products-catalog?category=" + quote(cat.name, safe=""), status_code=303)
    from app.care_memos import memo_form_context

    return templates.TemplateResponse(
        "admin_catalog_service_form.html",
        _ctx(
            request,
            current_user,
            err=err,
            is_new=False,
            category=cat,
            subcategory=sub,
            svc=svc,
            form_name=svc.name,
            form_short_name=svc.short_name or "",
            form_active=svc.is_active,
            form_jf=_fmt_price_input(svc.price_junior_from),
            form_jt=_fmt_price_input(svc.price_junior_to),
            form_mf=_fmt_price_input(svc.price_middle_from),
            form_mt=_fmt_price_input(svc.price_middle_to),
            form_sf=_fmt_price_input(svc.price_senior_from),
            form_st=_fmt_price_input(svc.price_senior_to),
            form_kit_override=(
                ""
                if svc.kit_section_override is None
                else ("1" if svc.kit_section_override else "0")
            ),
            form_tail_override=(
                ""
                if getattr(svc, "tail_section_override", None) is None
                else ("1" if svc.tail_section_override else "0")
            ),
            form_material_desc_override=(
                ""
                if getattr(svc, "material_description_override", None) is None
                else ("show" if svc.material_description_override else "hide")
            ),
            form_retail_kanekalon=svc.retail_material_kanekalon,
            form_retail_kudri=svc.retail_material_kudri,
            form_retail_mix=svc.retail_material_mix,
            form_duration=str(svc.estimated_duration_minutes),
            **memo_form_context(db, svc),
        ),
    )


@router.post("/services/{service_id}/edit")
def service_edit_save(
    service_id: int,
    name: str = Form(...),
    short_name: str | None = Form(None),
    is_active: str | None = Form(None),
    price_junior_from: str | None = Form(None),
    price_junior_to: str | None = Form(None),
    price_middle_from: str | None = Form(None),
    price_middle_to: str | None = Form(None),
    price_senior_from: str | None = Form(None),
    price_senior_to: str | None = Form(None),
    kit_section_override: str | None = Form(None),
    tail_section_override: str | None = Form(None),
    material_description_override: str | None = Form(None),
    retail_material_kanekalon: str | None = Form(None),
    retail_material_kudri: str | None = Form(None),
    retail_material_mix: str | None = Form(None),
    estimated_duration_minutes: str = Form(...),
    care_memo_before: str | None = Form(None),
    care_memo_after: str | None = Form(None),
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    svc = db.get(Service, service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail="Услуга не найдена")
    sub = db.get(ServiceSubcategory, svc.subcategory_id)
    cat = db.get(ServiceCategory, sub.category_id) if sub else None
    if cat and (cat.name or "").strip() in _PRODUCT_CATALOG_ONLY_CATEGORIES:
        return RedirectResponse(url="/products-catalog?category=" + quote(cat.name, safe=""), status_code=303)
    before = SimpleNamespace(
        name=svc.name,
        short_name=svc.short_name,
        is_active=svc.is_active,
        estimated_duration_minutes=svc.estimated_duration_minutes,
        price_junior_from=svc.price_junior_from,
        price_junior_to=svc.price_junior_to,
        price_middle_from=svc.price_middle_from,
        price_middle_to=svc.price_middle_to,
        price_senior_from=svc.price_senior_from,
        price_senior_to=svc.price_senior_to,
        kit_section_override=svc.kit_section_override,
        tail_section_override=getattr(svc, "tail_section_override", None),
        material_description_override=getattr(svc, "material_description_override", None),
        retail_material_kanekalon=svc.retail_material_kanekalon,
        retail_material_kudri=svc.retail_material_kudri,
        retail_material_mix=svc.retail_material_mix,
    )
    nm = (name or "").strip()
    if not nm:
        return RedirectResponse(
            url=f"/admin/catalog/services/{service_id}/edit?err=empty",
            status_code=303,
        )
    try:
        duration = _parse_estimated_duration(estimated_duration_minutes)
    except ValueError:
        return RedirectResponse(
            url=f"/admin/catalog/services/{service_id}/edit?err=bad_duration",
            status_code=303,
        )
    try:
        jf = _parse_optional_price(price_junior_from)
        jt = _parse_optional_price(price_junior_to)
        mf = _parse_optional_price(price_middle_from)
        mt = _parse_optional_price(price_middle_to)
        sf = _parse_optional_price(price_senior_from)
        st = _parse_optional_price(price_senior_to)
    except ValueError:
        return RedirectResponse(
            url=f"/admin/catalog/services/{service_id}/edit?err=bad_price",
            status_code=303,
        )

    svc.name = nm
    svc.short_name = _parse_optional_short_name(short_name, max_len=200)
    svc.is_active = _is_checked(is_active)
    svc.estimated_duration_minutes = duration
    svc.price_junior_from = jf
    svc.price_junior_to = jt
    svc.price_middle_from = mf
    svc.price_middle_to = mt
    svc.price_senior_from = sf
    svc.price_senior_to = st
    svc.kit_section_override = _parse_kit_section_override(kit_section_override)
    svc.tail_section_override = _parse_tail_section_override(tail_section_override)
    svc.material_description_override = _parse_tri_state(material_description_override)
    svc.retail_material_kanekalon = _is_checked(retail_material_kanekalon)
    svc.retail_material_kudri = _is_checked(retail_material_kudri)
    svc.retail_material_mix = _is_checked(retail_material_mix)
    from app.care_memos import apply_entity_memos

    try:
        apply_entity_memos(db, svc, care_memo_before, care_memo_after)
    except ValueError:
        return RedirectResponse(
            url=f"/admin/catalog/services/{service_id}/edit?err=memo",
            status_code=303,
        )
    svc.updated_at = utcnow_naive()
    svc.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=ServiceAuditLog,
        entity_field="service_id",
        entity_id=svc.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(
            before,
            svc,
            (
                "name",
                "short_name",
                "is_active",
                "estimated_duration_minutes",
                "price_junior_from",
                "price_junior_to",
                "price_middle_from",
                "price_middle_to",
                "price_senior_from",
                "price_senior_to",
                "kit_section_override",
                "tail_section_override",
                "material_description_override",
                "retail_material_kanekalon",
                "retail_material_kudri",
                "retail_material_mix",
            ),
        ),
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url=f"/admin/catalog/services/{service_id}/edit?err=duplicate",
            status_code=303,
        )
    return RedirectResponse(url=f"/admin/catalog/subcategories/{svc.subcategory_id}/services", status_code=303)
