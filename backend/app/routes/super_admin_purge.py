from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_admin_super_assigned
from app.db.session import get_db
from app.display_time import format_naive_utc_datetime, get_display_timezone
from app.super_admin_purge import (
    CONFIRM_PHRASE_1,
    CONFIRM_PHRASE_2,
    build_purge_preview,
    list_purge_history,
    parse_purge_entity,
    run_purge,
)
from app.webui import ctx as _ctx, templates

router = APIRouter(prefix="/admin/super", tags=["super_admin_purge"])
_SUPER = Depends(require_admin_super_assigned())


def _history_rows(db: Session) -> list[dict]:
    history = list_purge_history(db, limit=50)
    tz = get_display_timezone(db)
    return [
        {
            "when": format_naive_utc_datetime(h.purged_at, tz) or "—",
            "who": (h.actor_user.display_name if h.actor_user else None)
            or (f"#{h.actor_user_id}" if h.actor_user_id else "—"),
            "kind": h.entity_kind,
            "ids": h.entity_ids_text,
            "heading": h.heading or "—",
            "details": h.details_text or "",
        }
        for h in history
    ]


def _purge_page_ctx(request: Request, current_user: AuthUser, db: Session, *, error=None, msg=None):
    return _ctx(
        request,
        current_user=current_user,
        error=error,
        msg=msg,
        phrase1=CONFIRM_PHRASE_1,
        phrase2=CONFIRM_PHRASE_2,
        history_rows=_history_rows(db),
    )


@router.get("/purge/preview")
def super_purge_preview(
    entity: str = Query(""),
    entity_id: str = Query(""),
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    try:
        e, eid = parse_purge_entity(entity, entity_id)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)})
    data = build_purge_preview(db, e, eid)
    return JSONResponse(data)


@router.get("/purge", response_class=HTMLResponse)
def super_purge_form(
    request: Request,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
    msg: str | None = None,
):
    return templates.TemplateResponse(
        "admin_super_purge.html",
        _purge_page_ctx(request, current_user, db, msg=msg),
    )


@router.post("/purge", response_class=HTMLResponse)
async def super_purge_post(
    request: Request,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    form = await request.form()
    entity = str(form.get("entity") or "")
    entity_id_raw = str(form.get("entity_id") or "")
    confirm1 = str(form.get("confirm_phrase_1") or "")
    confirm2 = str(form.get("confirm_phrase_2") or "")

    if str(form.get("ack_irreversible") or "") != "on":
        return templates.TemplateResponse(
            "admin_super_purge.html",
            _purge_page_ctx(
                request,
                current_user,
                db,
                error="Нужно отметить: «Я понимаю, что восстановить данные будет нельзя».",
            ),
            status_code=400,
        )
    try:
        e, eid = parse_purge_entity(entity, entity_id_raw)
        run_purge(
            db,
            entity=e,
            entity_id=eid,
            confirm1=confirm1,
            confirm2=confirm2,
            actor_user_id=current_user.id,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return templates.TemplateResponse(
            "admin_super_purge.html",
            _purge_page_ctx(request, current_user, db, error=str(exc)),
            status_code=400,
        )
    return RedirectResponse(url="/admin/super/purge?msg=deleted", status_code=303)
