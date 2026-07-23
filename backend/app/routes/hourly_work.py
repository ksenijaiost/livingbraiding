from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth import AuthUser, require_role
from app.db.models import HourlyWorkEntry, UserRole
from app.db.session import get_db
from app.hourly_work import (
    can_access_hourly_work_entry,
    create_hourly_work_entry,
    duration_display,
    entry_to_form_prefill,
    list_masters_for_hourly_work_form,
    parse_hourly_work_form,
    update_hourly_work_entry,
)
from app.webui import ctx as _ctx
from app.webui import templates

router = APIRouter(prefix="/hourly-work", tags=["hourly-work"])

_ACCESS = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER))


def _is_admin_user(user: AuthUser) -> bool:
    return UserRole.ADMIN in user.roles or UserRole.ADMIN_SUPER in user.roles


def _list_entries(db: Session, current_user: AuthUser, *, limit: int = 100) -> list[HourlyWorkEntry]:
    stmt = (
        select(HourlyWorkEntry)
        .options(
            selectinload(HourlyWorkEntry.master_user),
            selectinload(HourlyWorkEntry.created_by_user),
        )
        .order_by(HourlyWorkEntry.performed_date.desc(), HourlyWorkEntry.id.desc())
        .limit(limit)
    )
    if UserRole.MASTER in current_user.roles and not _is_admin_user(current_user):
        stmt = stmt.where(HourlyWorkEntry.master_user_id == int(current_user.id))
    return list(db.scalars(stmt).all())


def _get_entry(db: Session, entry_id: int) -> HourlyWorkEntry | None:
    return db.scalar(
        select(HourlyWorkEntry)
        .options(
            selectinload(HourlyWorkEntry.master_user),
            selectinload(HourlyWorkEntry.created_by_user),
            selectinload(HourlyWorkEntry.work_plan),
        )
        .where(HourlyWorkEntry.id == entry_id)
    )


@router.get("", response_class=HTMLResponse)
def hourly_work_list(
    request: Request,
    current_user: AuthUser = _ACCESS,
    db: Session = Depends(get_db),
):
    rows = _list_entries(db, current_user)
    return templates.TemplateResponse(
        "hourly_work_list.html",
        _ctx(
            request,
            current_user=current_user,
            rows=rows,
            duration_display=duration_display,
            msg=request.query_params.get("msg"),
        ),
    )


@router.get("/new", response_class=HTMLResponse)
def hourly_work_new_get(
    request: Request,
    work_plan_id: str | None = Query(None),
    performed_date: str | None = Query(None),
    duration_h: str | None = Query(None),
    duration_m: str | None = Query(None),
    master_id: str | None = Query(None),
    comment: str | None = Query(None),
    current_user: AuthUser = _ACCESS,
    db: Session = Depends(get_db),
):
    is_admin = _is_admin_user(current_user)
    fp = {
        "performed_date": (performed_date or "").strip() or date.today().isoformat(),
        "duration_h": (duration_h or "").strip() or "0",
        "duration_m": (duration_m or "").strip() or "0",
        "comment": (comment or "").strip(),
    }
    if work_plan_id:
        fp["work_plan_id"] = work_plan_id.strip()
    if is_admin:
        if master_id:
            fp["master_id"] = master_id.strip()
    else:
        fp["master_id"] = str(current_user.id)
    return templates.TemplateResponse(
        "hourly_work_form.html",
        _ctx(
            request,
            current_user=current_user,
            fp=fp,
            masters=list_masters_for_hourly_work_form(db),
            is_admin=is_admin,
            error=None,
            from_work_plan=bool(fp.get("work_plan_id")),
        ),
    )


@router.post("/new")
async def hourly_work_new_post(
    request: Request,
    current_user: AuthUser = _ACCESS,
    db: Session = Depends(get_db),
):
    form = await request.form()
    is_admin = _is_admin_user(current_user)
    fp = {k: form.get(k) for k in form.keys()}
    entry, err = parse_hourly_work_form(
        form,
        current_user_id=int(current_user.id),
        is_admin=is_admin,
    )
    if err:
        return templates.TemplateResponse(
            "hourly_work_form.html",
            _ctx(
                request,
                current_user=current_user,
                fp={k: str(v) if v is not None else "" for k, v in fp.items()},
                masters=list_masters_for_hourly_work_form(db),
                is_admin=is_admin,
                error=err,
                from_work_plan=bool(fp.get("work_plan_id")),
            ),
            status_code=400,
        )
    assert entry is not None
    try:
        saved = create_hourly_work_entry(db, entry, created_by_user_id=int(current_user.id))
    except ValueError as exc:
        return templates.TemplateResponse(
            "hourly_work_form.html",
            _ctx(
                request,
                current_user=current_user,
                fp={k: str(v) if v is not None else "" for k, v in fp.items()},
                masters=list_masters_for_hourly_work_form(db),
                is_admin=is_admin,
                error=str(exc),
                from_work_plan=bool(fp.get("work_plan_id")),
            ),
            status_code=400,
        )
    if saved.work_plan_id:
        return RedirectResponse(
            url=f"/work-plans/{int(saved.work_plan_id)}?msg=hourly_created",
            status_code=303,
        )
    return RedirectResponse(url=f"/hourly-work/{int(saved.id)}?msg=created", status_code=303)


@router.get("/{entry_id}", response_class=HTMLResponse)
def hourly_work_detail(
    entry_id: int,
    request: Request,
    current_user: AuthUser = _ACCESS,
    db: Session = Depends(get_db),
):
    entry = _get_entry(db, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    is_admin = _is_admin_user(current_user)
    if not can_access_hourly_work_entry(
        entry, current_user_id=int(current_user.id), is_admin=is_admin
    ):
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return templates.TemplateResponse(
        "hourly_work_detail.html",
        _ctx(
            request,
            current_user=current_user,
            entry=entry,
            fp=entry_to_form_prefill(entry),
            masters=list_masters_for_hourly_work_form(db),
            is_admin=is_admin,
            duration_display=duration_display,
            error=None,
            msg=request.query_params.get("msg"),
        ),
    )


@router.post("/{entry_id}")
async def hourly_work_detail_save(
    entry_id: int,
    request: Request,
    current_user: AuthUser = _ACCESS,
    db: Session = Depends(get_db),
):
    entry = _get_entry(db, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    is_admin = _is_admin_user(current_user)
    if not can_access_hourly_work_entry(
        entry, current_user_id=int(current_user.id), is_admin=is_admin
    ):
        raise HTTPException(status_code=403, detail="Недостаточно прав")

    form = await request.form()
    fp = {k: form.get(k) for k in form.keys()}
    draft, err = parse_hourly_work_form(
        form,
        current_user_id=int(current_user.id),
        is_admin=is_admin,
    )
    if err or draft is None:
        return templates.TemplateResponse(
            "hourly_work_detail.html",
            _ctx(
                request,
                current_user=current_user,
                entry=entry,
                fp={k: str(v) if v is not None else "" for k, v in fp.items()},
                masters=list_masters_for_hourly_work_form(db),
                is_admin=is_admin,
                duration_display=duration_display,
                error=err or "Некорректные данные.",
                msg=None,
            ),
            status_code=400,
        )
    try:
        update_hourly_work_entry(
            db,
            entry,
            draft,
            updated_by_user_id=int(current_user.id),
            is_admin=is_admin,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "hourly_work_detail.html",
            _ctx(
                request,
                current_user=current_user,
                entry=entry,
                fp={k: str(v) if v is not None else "" for k, v in fp.items()},
                masters=list_masters_for_hourly_work_form(db),
                is_admin=is_admin,
                duration_display=duration_display,
                error=str(exc),
                msg=None,
            ),
            status_code=400,
        )
    return RedirectResponse(url=f"/hourly-work/{entry_id}?msg=saved", status_code=303)
