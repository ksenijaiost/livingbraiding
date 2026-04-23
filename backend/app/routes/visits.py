from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import AuthUser, require_role
from app.time_utils import utcnow_naive
from app.db.session import get_db
from app.db.models import (
    Client,
    Kit,
    User,
    UserRole,
    Visit,
    VisitAuditLog,
    VisitKitUsage,
    VisitMaster,
)
from app.payroll_fund import storno_source_accruals
from app.payroll_fund import PayrollFundSourceKind
from app.forms_parse import parse_bool, parse_int
from app.ui_visit_display import (
    build_service_human_display,
    kit_usages_empty_explanation,
    ru_mix_complexity,
    ru_mix_source,
)
from app.visit_edit_policy import is_in_closed_payroll_period, visit_client_change_policy
from app.audit import diff_fields, write_audit_rows
from app.webui import templates, ctx as _ctx


router = APIRouter()


def _redirect_admin_visits_to_canon(request: Request, *, visit_id: int | None = None) -> RedirectResponse:
    """Старые URL под /admin/visits → канон /visits (GET, постоянный редирект)."""
    if visit_id is None:
        new_path = "/visits"
    else:
        new_path = f"/visits/{int(visit_id)}"
    target = str(request.url.replace(path=new_path))
    return RedirectResponse(url=target, status_code=308)


@router.get("/admin/visits", response_class=HTMLResponse)
def admin_visits_legacy_redirect(
    request: Request,
    current_user=Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
):
    return _redirect_admin_visits_to_canon(request)


@router.get("/visits", response_class=HTMLResponse)
def admin_visits(
    request: Request,
    mine: str | None = Query(None),
    current_user=Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    mine_raw = (mine or "").strip().lower()
    visits_mine_only = mine_raw in ("1", "true", "yes", "only")
    stmt = select(Visit).options(selectinload(Visit.client), selectinload(Visit.services))
    if visits_mine_only:
        stmt = stmt.where(
            or_(
                Visit.id.in_(select(VisitMaster.visit_id).where(VisitMaster.master_id == current_user.id)),
                Visit.mix_bonus_master_id == current_user.id,
            )
        )
    stmt = stmt.order_by(Visit.performed_date.desc()).limit(200)
    visits = list(db.scalars(stmt).all())
    return templates.TemplateResponse(
        "admin_visits.html",
        _ctx(
            request,
            current_user=current_user,
            visits=visits,
            visits_mine_only=visits_mine_only,
        ),
    )


@router.get("/admin/visits/{visit_id}", response_class=HTMLResponse)
def admin_visit_detail_legacy_redirect(
    visit_id: int,
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
):
    return _redirect_admin_visits_to_canon(request, visit_id=visit_id)


@router.get("/visits/{visit_id}", response_class=HTMLResponse)
def admin_visit_detail(
    visit_id: int,
    request: Request,
    client_err: str | None = None,
    msg: str | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    visit = db.scalar(
        select(Visit)
        .options(
            selectinload(Visit.client),
            selectinload(Visit.services),
            selectinload(Visit.kit_usages).selectinload(VisitKitUsage.kit),
            selectinload(Visit.masters).selectinload(VisitMaster.master),
        )
        .where(Visit.id == visit_id)
    )
    if not visit:
        raise HTTPException(status_code=404, detail="Визит не найден")

    audit_rows = list(
        db.scalars(
            select(VisitAuditLog)
            .options(selectinload(VisitAuditLog.changed_by_user))
            .where(VisitAuditLog.visit_id == visit_id)
            .order_by(VisitAuditLog.changed_at.desc(), VisitAuditLog.id.desc())
            .limit(200)
        ).all()
    )

    service_displays = [build_service_human_display(vs) for vs in visit.services]

    mix_bonus_master_label: str | None = None
    if visit.mix_bonus_master_id:
        u = db.get(User, visit.mix_bonus_master_id)
        if u and (u.display_name or "").strip():
            mix_bonus_master_label = u.display_name.strip()
        else:
            mix_bonus_master_label = f"ID {visit.mix_bonus_master_id}"

    visit_creator_label: str | None = None
    if visit.created_by_user_id:
        cu = db.get(User, visit.created_by_user_id)
        if cu and (cu.display_name or "").strip():
            visit_creator_label = cu.display_name.strip()

    duration_h = visit.duration_minutes // 60
    duration_m = visit.duration_minutes % 60

    v_policy = visit_client_change_policy(visit, current_user, db)

    return templates.TemplateResponse(
        "admin_visit_detail.html",
        _ctx(
            request,
            current_user=current_user,
            visit=visit,
            audit_rows=audit_rows,
            service_displays=service_displays,
            mix_bonus_master_label=mix_bonus_master_label,
            mix_source_ru=ru_mix_source(visit.mix_source),
            mix_complexity_ru=ru_mix_complexity(getattr(visit, "mix_complexity", None)),
            materials_used_ru="Да" if (visit.kanekalon_grams > 0 or visit.kudri_grams > 0) else "Нет",
            kit_usages_note=kit_usages_empty_explanation(),
            visit_creator_label=visit_creator_label,
            duration_h=duration_h,
            duration_m=duration_m,
            client_err=client_err,
            msg=msg,
            visit_edit_blocked=not v_policy.can_change,
            visit_edit_block_msg=v_policy.message_when_blocked,
        ),
    )


def _visit_cancel_revert_stock(db: Session, visit: Visit) -> tuple[bool, str]:
    """Revert stock kit usages for a visit. Two-pass: validate then apply."""
    usages = list(getattr(visit, "kit_usages", []) or [])
    if not usages:
        return True, ""
    kit_rows: list[tuple[Kit, int]] = []
    for u in usages:
        kit = getattr(u, "kit", None) or db.get(Kit, u.kit_id)
        if not kit:
            return False, "Не найден комплект для отката списания (kit_id)."
        pieces = int(u.pieces_used or 0)
        if pieces <= 0:
            continue
        new_avail = int(kit.pieces_available + pieces)
        if int(kit.pieces_total) >= 0 and new_avail > int(kit.pieces_total):
            return (
                False,
                f"Нельзя отменить визит: возврат превысит остаток 'всего' по комплекту {kit.sku}.",
            )
        kit_rows.append((kit, pieces))
    for kit, pieces in kit_rows:
        kit.pieces_available = int(kit.pieces_available + pieces)
        if kit.pieces_available > 0:
            kit.is_in_stock = True
    return True, ""


@router.post("/visits/{visit_id}/cancel")
@router.post("/admin/visits/{visit_id}/cancel")
async def admin_visit_cancel(
    visit_id: int,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    visit = db.scalar(
        select(Visit)
        .options(selectinload(Visit.kit_usages).selectinload(VisitKitUsage.kit))
        .where(Visit.id == visit_id)
    )
    if not visit:
        raise HTTPException(status_code=404, detail="Визит не найден")
    if visit.is_cancelled:
        return RedirectResponse(url=f"/visits/{visit_id}?msg=already_cancelled", status_code=303)
    if is_in_closed_payroll_period(db, visit.created_at):
        return RedirectResponse(url=f"/visits/{visit_id}?msg=cancel_closed_period", status_code=303)

    ok, err = _visit_cancel_revert_stock(db, visit)
    if not ok:
        return RedirectResponse(url=f"/visits/{visit_id}?msg=cancel_conflict", status_code=303)

    before = SimpleNamespace(
        is_cancelled=visit.is_cancelled,
        cancelled_at=visit.cancelled_at,
        cancelled_by_user_id=visit.cancelled_by_user_id,
    )
    visit.is_cancelled = True
    visit.cancelled_at = utcnow_naive()
    visit.cancelled_by_user_id = current_user.id
    visit.updated_at = utcnow_naive()
    visit.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=VisitAuditLog,
        entity_field="visit_id",
        entity_id=visit.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before, visit, ("is_cancelled", "cancelled_at", "cancelled_by_user_id")),
    )
    storno_source_accruals(db, PayrollFundSourceKind.VISIT, visit.id, current_user.id)
    db.commit()
    return RedirectResponse(url=f"/visits/{visit_id}?msg=cancelled", status_code=303)


@router.post("/visits/{visit_id}/client")
@router.post("/admin/visits/{visit_id}/client")
async def admin_visit_change_client(
    visit_id: int,
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    visit = db.get(Visit, visit_id)
    if visit is None:
        raise HTTPException(status_code=404, detail="Визит не найден")

    policy = visit_client_change_policy(visit, current_user, db)
    if not policy.can_change:
        raise HTTPException(status_code=403, detail=policy.message_when_blocked or "Недостаточно прав")

    form = await request.form()
    raw = form.get("new_client_id")
    try:
        new_cid = parse_int(raw, min=1, field_name="new_client_id")
    except ValueError:
        return RedirectResponse(url=f"/visits/{visit_id}?client_err=bad_id", status_code=303)

    confirm_late = parse_bool(form.get("confirm_late"))
    if policy.super_outside_window and not confirm_late:
        return RedirectResponse(url=f"/visits/{visit_id}?client_err=need_confirm", status_code=303)

    if new_cid == visit.client_id:
        return RedirectResponse(url=f"/visits/{visit_id}?client_err=same", status_code=303)
    new_client = db.get(Client, new_cid)
    if new_client is None:
        return RedirectResponse(url=f"/visits/{visit_id}?client_err=not_found", status_code=303)

    old_id = visit.client_id
    visit.client_id = new_cid
    visit.client_age_group = new_client.age_group
    visit.updated_at = utcnow_naive()
    visit.updated_by_user_id = current_user.id
    db.add(
        VisitAuditLog(
            visit_id=visit.id,
            changed_by_user_id=current_user.id,
            field_name="client_id",
            old_value=str(old_id),
            new_value=str(new_cid),
        )
    )
    db.commit()
    return RedirectResponse(url=f"/visits/{visit_id}", status_code=303)

