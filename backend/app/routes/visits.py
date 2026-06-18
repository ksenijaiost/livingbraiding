from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import AuthUser, require_assigned_roles, require_role
from app.time_utils import utcnow_naive
from app.db.session import get_db
from app.db.models import (
    Client,
    User,
    UserRole,
    Visit,
    VisitAuditLog,
    VisitKitUsage,
    VisitMaster,
    VisitService,
)
from app.payroll_fund import storno_source_accruals
from app.payroll_fund import PayrollFundSourceKind
from app.forms_parse import parse_bool, parse_int
from app.ui_visit_display import (
    build_service_human_display,
    kit_usages_empty_explanation,
    ru_mix_complexity,
    ru_mix_source,
    visit_services_catalog_line,
)
from app.visit_multi_service import (
    form_uses_multi_service_lines,
    kit_inlay_to_multi,
    parse_multi_service_visit_form,
    recalc_visit_totals,
    update_visit_with_services,
)
from app.visit_edit_policy import is_in_closed_payroll_period, visit_edit_policy
from app.visit_form_prefill import visit_to_form_prefill
from app.visit_stock import visit_cancel_revert_stock, visit_service_revert_stock
from app.audit import diff_fields, write_audit_rows
from app.kit_inlay_visit import (
    collect_questionnaire_prefill_from_form,
    parse_kit_inlay_form,
)
from app.media_store import get_nonempty_upload, save_upload_image
from app.routes.master_visit import _master_visit_step1_template_response, _visit_master_state_from_prefill
from app.thermo_visit import collect_thermo_prefill_from_form
from app.webui import templates, ctx as _ctx


router = APIRouter()


def _visits_list_url(*, mine_only: bool, show_cancelled: bool) -> str:
    q: dict[str, str] = {}
    if mine_only:
        q["mine"] = "1"
    if show_cancelled:
        q["show_cancelled"] = "1"
    return "/visits" + ("?" + urlencode(q) if q else "")


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
    show_cancelled: str | None = Query(None),
    current_user=Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    mine_raw = (mine or "").strip().lower()
    visits_mine_only = mine_raw in ("1", "true", "yes", "only")
    visits_show_cancelled = parse_bool(show_cancelled)
    stmt = select(Visit).options(selectinload(Visit.client), selectinload(Visit.services))
    if not visits_show_cancelled:
        stmt = stmt.where(Visit.is_cancelled.is_(False))
    if visits_mine_only:
        from app.db.models import VisitServiceMaster

        stmt = stmt.where(
            or_(
                Visit.id.in_(select(VisitMaster.visit_id).where(VisitMaster.master_id == current_user.id)),
                Visit.id.in_(
                    select(VisitService.visit_id).where(
                        VisitService.is_cancelled.is_(False),
                        VisitService.mix_bonus_master_id == current_user.id,
                    )
                ),
                Visit.id.in_(
                    select(VisitService.visit_id)
                    .join(VisitServiceMaster, VisitServiceMaster.visit_service_id == VisitService.id)
                    .where(
                        VisitService.is_cancelled.is_(False),
                        VisitServiceMaster.master_id == current_user.id,
                    )
                ),
                Visit.mix_bonus_master_id == current_user.id,
            )
        )
    stmt = stmt.order_by(Visit.performed_date.desc()).limit(200)
    visits = list(db.scalars(stmt).all())
    for v in visits:
        v.services_line = visit_services_catalog_line(v)  # type: ignore[attr-defined]
    return templates.TemplateResponse(
        "admin_visits.html",
        _ctx(
            request,
            current_user=current_user,
            visits=visits,
            visits_mine_only=visits_mine_only,
            visits_show_cancelled=visits_show_cancelled,
            visits_url_scope_all=_visits_list_url(mine_only=False, show_cancelled=visits_show_cancelled),
            visits_url_scope_mine=_visits_list_url(mine_only=True, show_cancelled=visits_show_cancelled),
            visits_url_active_only=_visits_list_url(mine_only=visits_mine_only, show_cancelled=False),
            visits_url_include_cancelled=_visits_list_url(mine_only=visits_mine_only, show_cancelled=True),
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

    sorted_services = sorted(visit.services or [], key=lambda s: (int(s.sort_order or 0), int(s.id or 0)))
    service_displays = {vs.id: build_service_human_display(vs) for vs in sorted_services}

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

    v_policy = visit_edit_policy(visit, current_user, db)
    visit_closed_period = is_in_closed_payroll_period(db, visit.created_at)
    visit_super_priv = UserRole.ADMIN_SUPER in current_user.roles or UserRole.TECHSPEC in current_user.roles

    return templates.TemplateResponse(
        "admin_visit_detail.html",
        _ctx(
            request,
            current_user=current_user,
            visit=visit,
            audit_rows=audit_rows,
            service_displays=service_displays,
            sorted_services=sorted_services,
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
            visit_edit_blocked=not v_policy.can_edit,
            visit_edit_block_msg=v_policy.message_when_blocked,
            visit_client_change_confirm_required=False,
            visit_closed_period=visit_closed_period,
            visit_super_priv=visit_super_priv,
        ),
    )


def _load_visit_for_edit(db: Session, visit_id: int) -> Visit | None:
    return db.scalar(
        select(Visit)
        .options(
            selectinload(Visit.client),
            selectinload(Visit.services),
            selectinload(Visit.kit_usages).selectinload(VisitKitUsage.kit),
            selectinload(Visit.masters),
        )
        .where(Visit.id == visit_id)
    )


@router.get("/visits/{visit_id}/edit", response_class=HTMLResponse)
def admin_visit_edit_get(
    visit_id: int,
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    visit = _load_visit_for_edit(db, visit_id)
    if not visit:
        raise HTTPException(status_code=404, detail="Визит не найден")
    policy = visit_edit_policy(visit, current_user, db)
    if not policy.can_edit:
        return _master_visit_step1_template_response(
            request,
            current_user=current_user,
            db=db,
            form_prefill={},
            visit_master_on_ids=[],
            visit_master_pct_str={},
            error=policy.message_when_blocked or "Редактирование запрещено.",
            status_code=403,
            is_edit=True,
            edit_visit_id=visit_id,
            cancel_url=f"/visits/{visit_id}",
        )
    fp, vm_on_ids, vm_pct_str, _extra = visit_to_form_prefill(db, visit)
    vm_on_ids, vm_pct_str = _visit_master_state_from_prefill(fp)
    selected_client = visit.client
    photos = [p for p in (visit.photo_1, visit.photo_2, visit.photo_3) if p]
    err = request.query_params.get("err")
    return _master_visit_step1_template_response(
        request,
        current_user=current_user,
        db=db,
        form_prefill=fp,
        visit_master_on_ids=vm_on_ids,
        visit_master_pct_str=vm_pct_str,
        selected_client=selected_client,
        error=err,
        is_edit=True,
        edit_visit_id=visit_id,
        cancel_url=f"/visits/{visit_id}",
        visit_photos=photos,
    )


def _visit_edit_form_error_response(
    request: Request,
    *,
    current_user: AuthUser,
    db: Session,
    visit_id: int,
    form: Any,
    error: str,
):
    fp = {}
    for key in form.keys():
        last = None
        for v in form.getlist(key):
            if hasattr(v, "read"):
                continue
            last = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
        if last is not None:
            fp[key] = last
    fp.update(collect_questionnaire_prefill_from_form(form))
    fp.update(collect_thermo_prefill_from_form(form))
    vm_on_ids, vm_pct_str = _visit_master_state_from_prefill(fp)
    selected_client = None
    eid = (fp.get("existing_client_id") or "").strip()
    try:
        eid_int = parse_int(eid, min=1, field_name="existing_client_id")
    except ValueError:
        eid_int = 0
    if eid_int > 0:
        selected_client = db.get(Client, eid_int)
    visit = db.get(Visit, visit_id)
    photos = []
    if visit:
        photos = [p for p in (visit.photo_1, visit.photo_2, visit.photo_3) if p]
    return _master_visit_step1_template_response(
        request,
        current_user=current_user,
        db=db,
        form_prefill=fp,
        visit_master_on_ids=vm_on_ids,
        visit_master_pct_str=vm_pct_str,
        selected_client=selected_client,
        error=error,
        status_code=400,
        is_edit=True,
        edit_visit_id=visit_id,
        cancel_url=f"/visits/{visit_id}",
        visit_photos=photos,
    )


@router.post("/visits/{visit_id}/edit")
async def admin_visit_edit_post(
    visit_id: int,
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    visit = _load_visit_for_edit(db, visit_id)
    if not visit:
        raise HTTPException(status_code=404, detail="Визит не найден")
    policy = visit_edit_policy(visit, current_user, db)
    if not policy.can_edit:
        raise HTTPException(status_code=403, detail=policy.message_when_blocked or "Недостаточно прав")

    form = await request.form()
    try:
        if form_uses_multi_service_lines(form):
            multi = parse_multi_service_visit_form(form, booking_id=visit.booking_id)
            update_visit_with_services(db, visit_id, current_user.id, multi)
        else:
            kinp = parse_kit_inlay_form(form, single_master_default_id=current_user.id)
            multi = kit_inlay_to_multi(kinp, booking_id=visit.booking_id)
            update_visit_with_services(db, visit_id, current_user.id, multi)
        visit = db.get(Visit, visit_id)
        assert visit is not None
        try:
            p1 = get_nonempty_upload(form, "photo_1")
            p2 = get_nonempty_upload(form, "photo_2")
            p3 = get_nonempty_upload(form, "photo_3")
            if p1 is not None:
                visit.photo_1 = await save_upload_image(p1)
            if p2 is not None:
                visit.photo_2 = await save_upload_image(p2)
            if p3 is not None:
                visit.photo_3 = await save_upload_image(p3)
            if p1 or p2 or p3:
                db.commit()
        except ValueError as exc:
            db.rollback()
            return _visit_edit_form_error_response(
                request,
                current_user=current_user,
                db=db,
                visit_id=visit_id,
                form=form,
                error=str(exc),
            )
    except ValueError as exc:
        db.rollback()
        return _visit_edit_form_error_response(
            request,
            current_user=current_user,
            db=db,
            visit_id=visit_id,
            form=form,
            error=str(exc),
        )
    return RedirectResponse(url=f"/visits/{visit_id}?msg=updated", status_code=303)


def _visit_cancel_revert_stock(db: Session, visit: Visit) -> tuple[bool, str]:
    return visit_cancel_revert_stock(db, visit)


def _visit_service_cancel_revert_stock(db: Session, visit_service_id: int) -> tuple[bool, str]:
    return visit_service_revert_stock(db, visit_service_id)


@router.post("/visits/{visit_id}/cancel")
@router.post("/admin/visits/{visit_id}/cancel")
async def admin_visit_cancel(
    visit_id: int,
    current_user: AuthUser = Depends(require_assigned_roles(UserRole.ADMIN_SUPER)),
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
    for vs in db.scalars(select(VisitService).where(VisitService.visit_id == visit.id)).all():
        storno_source_accruals(db, PayrollFundSourceKind.VISIT_SERVICE, vs.id, current_user.id)
    db.commit()
    return RedirectResponse(url=f"/visits/{visit_id}?msg=cancelled", status_code=303)


@router.post("/visits/{visit_id}/services/{vs_id}/cancel")
async def admin_visit_service_cancel(
    visit_id: int,
    vs_id: int,
    current_user: AuthUser = Depends(require_assigned_roles(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    visit = db.scalar(
        select(Visit)
        .options(selectinload(Visit.services), selectinload(Visit.kit_usages))
        .where(Visit.id == visit_id)
    )
    if not visit:
        raise HTTPException(status_code=404, detail="Визит не найден")
    vs = db.get(VisitService, vs_id)
    if not vs or vs.visit_id != visit.id:
        raise HTTPException(status_code=404, detail="Услуга визита не найдена")
    if visit.is_cancelled:
        return RedirectResponse(url=f"/visits/{visit_id}?msg=already_cancelled", status_code=303)
    if vs.is_cancelled:
        return RedirectResponse(url=f"/visits/{visit_id}?msg=service_already_cancelled", status_code=303)
    if is_in_closed_payroll_period(db, visit.created_at):
        return RedirectResponse(url=f"/visits/{visit_id}?msg=cancel_closed_period", status_code=303)

    ok, _err = _visit_service_cancel_revert_stock(db, vs.id)
    if not ok:
        return RedirectResponse(url=f"/visits/{visit_id}?msg=cancel_conflict", status_code=303)

    storno_source_accruals(db, PayrollFundSourceKind.VISIT_SERVICE, vs.id, current_user.id)
    vs.is_cancelled = True
    vs.cancelled_at = utcnow_naive()
    vs.cancelled_by_user_id = current_user.id
    recalc_visit_totals(visit)
    visit.updated_at = utcnow_naive()
    visit.updated_by_user_id = current_user.id
    db.commit()
    return RedirectResponse(url=f"/visits/{visit_id}?msg=service_cancelled", status_code=303)


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

    policy = visit_edit_policy(visit, current_user, db)
    if not policy.can_edit:
        raise HTTPException(status_code=403, detail=policy.message_when_blocked or "Недостаточно прав")

    form = await request.form()
    raw = form.get("new_client_id")
    try:
        new_cid = parse_int(raw, min=1, field_name="new_client_id")
    except ValueError:
        return RedirectResponse(url=f"/visits/{visit_id}/edit?err=bad_client_id", status_code=303)

    if new_cid == visit.client_id:
        return RedirectResponse(url=f"/visits/{visit_id}/edit?err=same_client", status_code=303)
    new_client = db.get(Client, new_cid)
    if new_client is None:
        return RedirectResponse(url=f"/visits/{visit_id}/edit?err=client_not_found", status_code=303)

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
    return RedirectResponse(url=f"/visits/{visit_id}?msg=client_changed", status_code=303)

