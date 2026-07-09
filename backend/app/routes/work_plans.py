"""Планы работ — список, создание, карточка."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth import AuthUser, require_role
from app.db.models import User, UserRole, WorkKind, WorkPlan, WorkPlanStatus, WorkPlanType
from app.db.session import get_db
from app.display_time import get_display_timezone
from app.forms_parse import parse_bool, parse_date_iso, parse_int
from app.list_search import parse_list_id_search
from app.time_utils import utcnow_naive
from app.webui import ctx as _ctx, templates
from app.work_plan import (
    is_master_available_for_work_plan,
    linked_work_for_plan,
    list_masters_for_work_plan_form,
    parse_duration_minutes,
    parse_planned_datetime,
    planned_end_local,
    planned_local_datetime,
    validate_work_plan_interval,
    work_kind_label,
    work_plan_is_open,
    work_plan_status_label,
    work_plan_type_display,
    work_plan_work_new_query_params,
)

router = APIRouter(prefix="/work-plans", tags=["work-plans"])

_VIEW = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER))
_CREATE = _VIEW


def _is_admin(user: AuthUser) -> bool:
    return UserRole.ADMIN in user.roles or UserRole.ADMIN_SUPER in user.roles


def _masters_for_form(db: Session) -> list[User]:
    return list_masters_for_work_plan_form(db)


def _kind_options() -> list[dict[str, str]]:
    return [{"value": k.value, "label": work_kind_label(k)} for k in WorkKind]


@router.get("", response_class=HTMLResponse)
def work_plans_list(
    request: Request,
    show: str | None = None,
    mine: str | None = Query(None),
    sort_date: str | None = Query(None),
    q: str | None = Query(None),
    current_user: AuthUser = _VIEW,
    db: Session = Depends(get_db),
):
    show_mode = (show or "").strip().lower() or "active"
    list_search_q = (q or "").strip()
    search_id = parse_list_id_search(list_search_q)
    mine_raw = (mine or "").strip().lower()
    sort_date_raw = (sort_date or "").strip().lower()
    sort_date_mode = "desc" if sort_date_raw == "desc" else "asc"
    if _is_admin(current_user):
        mine_only = mine_raw in ("1", "true", "yes", "only")
    else:
        mine_only = mine_raw not in ("0", "false", "no", "all")

    stmt = select(WorkPlan).options(selectinload(WorkPlan.master))
    if show_mode != "all":
        stmt = stmt.where(WorkPlan.status == WorkPlanStatus.PLANNED)
    if mine_only:
        stmt = stmt.where(WorkPlan.master_id == current_user.id)
    if search_id is not None:
        stmt = stmt.where(WorkPlan.id == search_id)
    if sort_date_mode == "desc":
        stmt = stmt.order_by(WorkPlan.planned_date.desc(), WorkPlan.id.desc())
    else:
        stmt = stmt.order_by(WorkPlan.planned_date.asc(), WorkPlan.id.asc())
    rows = list(db.scalars(stmt.limit(200)).all())

    display_tz = get_display_timezone(db)
    return templates.TemplateResponse(
        "work_plans_list.html",
        _ctx(
            request,
            current_user=current_user,
            rows=rows,
            show_mode=show_mode,
            mine_only=mine_only,
            sort_date_mode=sort_date_mode,
            list_search_q=list_search_q,
            search_id=search_id,
            display_tz=display_tz,
            work_plan_status_label=work_plan_status_label,
            work_plan_type_display=work_plan_type_display,
        ),
    )


@router.get("/new", response_class=HTMLResponse)
def work_plan_new_get(
    request: Request,
    current_user: AuthUser = _CREATE,
    db: Session = Depends(get_db),
):
    tz = get_display_timezone(db)
    is_admin = _is_admin(current_user)
    fp = {
        "planned_date": date.today().isoformat(),
        "planned_time": "",
        "duration_h": "1",
        "duration_m": "0",
        "plan_type": WorkPlanType.WORK_PRODUCT.value,
        "work_kind": WorkKind.KIT.value,
        "master_id": str(current_user.id),
        "pick_other_master": "0",
        "comment": "",
    }
    return templates.TemplateResponse(
        "work_plan_form.html",
        _ctx(
            request,
            current_user=current_user,
            error=None,
            fp=fp,
            masters=_masters_for_form(db),
            kinds=_kind_options(),
            is_admin=is_admin,
            display_tz=tz,
        ),
    )


@router.post("/new")
async def work_plan_new_post(
    request: Request,
    current_user: AuthUser = _CREATE,
    db: Session = Depends(get_db),
):
    form = await request.form()
    fp = {k: str(v) for k, v in form.items() if isinstance(k, str)}
    is_admin = _is_admin(current_user)
    tz = get_display_timezone(db)
    try:
        local_day = parse_date_iso(fp.get("planned_date", ""), field_name="planned_date")
        planned_utc = parse_planned_datetime(local_day, fp.get("planned_time", ""), tz)
        duration = parse_duration_minutes(
            int(fp.get("duration_h") or "0"),
            int(fp.get("duration_m") or "0"),
        )
        plan_type_raw = (fp.get("plan_type") or WorkPlanType.WORK_PRODUCT.value).strip().upper()
        if plan_type_raw == WorkPlanType.HOURLY.value:
            raise ValueError("Почасовая работа пока в разработке.")
        plan_type = WorkPlanType.WORK_PRODUCT
        kind_raw = (fp.get("work_kind") or "").strip().upper()
        if not kind_raw:
            raise ValueError("Выберите вид работы.")
        work_kind = WorkKind(kind_raw)
        if is_admin or parse_bool(fp.get("pick_other_master", "")):
            mid = parse_int(fp.get("master_id", "0"), min=1, field_name="master_id")
        else:
            mid = current_user.id
        master = db.get(User, mid)
        if master is None or not master.is_active:
            raise ValueError("Мастер не найден.")
        err = validate_work_plan_interval(
            db, master_id=mid, start_utc=planned_utc, duration_minutes=duration
        )
        if err:
            raise ValueError(err)
        comment = (fp.get("comment") or "").strip() or None
        plan = WorkPlan(
            created_by_user_id=current_user.id,
            planned_date=planned_utc,
            duration_minutes=duration,
            master_id=mid,
            plan_type=plan_type,
            work_kind=work_kind,
            comment=comment,
            status=WorkPlanStatus.PLANNED,
        )
        db.add(plan)
        db.commit()
        return RedirectResponse(url=f"/work-plans/{plan.id}?msg=created", status_code=303)
    except ValueError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "work_plan_form.html",
            _ctx(
                request,
                current_user=current_user,
                error=str(exc),
                fp=fp,
                masters=_masters_for_form(db),
                kinds=_kind_options(),
                is_admin=is_admin,
                display_tz=tz,
            ),
            status_code=400,
        )


@router.get("/{plan_id}", response_class=HTMLResponse)
def work_plan_detail(
    plan_id: int,
    request: Request,
    msg: str | None = None,
    edit_time: str | None = None,
    err: str | None = None,
    current_user: AuthUser = _VIEW,
    db: Session = Depends(get_db),
):
    plan = db.scalar(
        select(WorkPlan)
        .options(
            selectinload(WorkPlan.master),
            selectinload(WorkPlan.created_by_user),
            selectinload(WorkPlan.cancelled_by_user),
        )
        .where(WorkPlan.id == plan_id)
    )
    if plan is None:
        raise HTTPException(status_code=404, detail="План не найден")
    tz = get_display_timezone(db)
    local_start = planned_local_datetime(plan, tz)
    local_end = planned_end_local(plan, tz)
    linked_work = linked_work_for_plan(db, plan.id)
    is_assigned_master = int(plan.master_id) == current_user.id
    can_create_work = work_plan_is_open(plan) and is_assigned_master
    can_edit_time = work_plan_is_open(plan) and (
        is_assigned_master or _is_admin(current_user) or int(plan.created_by_user_id) == current_user.id
    )
    work_new_url = None
    if can_create_work:
        work_new_url = "/sales/work/new?" + urlencode(work_plan_work_new_query_params(db, plan))
    return templates.TemplateResponse(
        "work_plan_detail.html",
        _ctx(
            request,
            current_user=current_user,
            plan=plan,
            msg=msg,
            err=err,
            display_tz=tz,
            local_start=local_start,
            local_end=local_end,
            duration_h=plan.duration_minutes // 60,
            duration_m=plan.duration_minutes % 60,
            work_plan_status_label=work_plan_status_label,
            work_plan_type_display=work_plan_type_display,
            linked_work=linked_work,
            can_create_work=can_create_work,
            can_edit_time=can_edit_time,
            edit_time_mode=edit_time == "1",
            work_new_url=work_new_url,
        ),
    )


@router.post("/{plan_id}/cancel")
async def work_plan_cancel(
    plan_id: int,
    current_user: AuthUser = _VIEW,
    db: Session = Depends(get_db),
):
    plan = db.get(WorkPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="План не найден")
    if not work_plan_is_open(plan):
        return RedirectResponse(url=f"/work-plans/{plan_id}?msg=already_closed", status_code=303)
    plan.status = WorkPlanStatus.CANCELLED
    plan.cancelled_at = utcnow_naive()
    plan.cancelled_by_user_id = current_user.id
    plan.updated_at = utcnow_naive()
    plan.updated_by_user_id = current_user.id
    db.commit()
    return RedirectResponse(url=f"/work-plans/{plan_id}?msg=cancelled", status_code=303)


@router.post("/{plan_id}/edit-time")
async def work_plan_edit_time(
    plan_id: int,
    request: Request,
    current_user: AuthUser = _VIEW,
    db: Session = Depends(get_db),
):
    plan = db.get(WorkPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="План не найден")
    is_assigned = int(plan.master_id) == current_user.id
    if not work_plan_is_open(plan) or not (
        is_assigned or _is_admin(current_user) or int(plan.created_by_user_id) == current_user.id
    ):
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    form = await request.form()
    tz = get_display_timezone(db)
    try:
        local_day = parse_date_iso(str(form.get("planned_date") or ""), field_name="planned_date")
        planned_utc = parse_planned_datetime(local_day, str(form.get("planned_time") or ""), tz)
        duration = parse_duration_minutes(
            int(str(form.get("duration_h") or "0")),
            int(str(form.get("duration_m") or "0")),
        )
        err = validate_work_plan_interval(
            db,
            master_id=int(plan.master_id),
            start_utc=planned_utc,
            duration_minutes=duration,
            exclude_plan_id=plan.id,
        )
        if err:
            raise ValueError(err)
        plan.planned_date = planned_utc
        plan.duration_minutes = duration
        plan.updated_at = utcnow_naive()
        plan.updated_by_user_id = current_user.id
        db.commit()
        return RedirectResponse(url=f"/work-plans/{plan_id}?msg=updated", status_code=303)
    except ValueError as exc:
        db.rollback()
        from urllib.parse import quote

        return RedirectResponse(
            url=f"/work-plans/{plan_id}?edit_time=1&err={quote(str(exc))}",
            status_code=303,
        )
