from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_admin_super_assigned
from app.db.session import get_db
from app.super_admin_purge import CONFIRM_PHRASE_1, CONFIRM_PHRASE_2, parse_purge_entity, run_purge
from app.webui import ctx as _ctx, templates

router = APIRouter(prefix="/admin/super", tags=["super_admin_purge"])
_SUPER = Depends(require_admin_super_assigned())


@router.get("/purge", response_class=HTMLResponse)
def super_purge_form(
    request: Request,
    current_user: AuthUser = _SUPER,
    msg: str | None = None,
):
    return templates.TemplateResponse(
        "admin_super_purge.html",
        _ctx(
            request,
            current_user=current_user,
            error=None,
            msg=msg,
            phrase1=CONFIRM_PHRASE_1,
            phrase2=CONFIRM_PHRASE_2,
        ),
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
            _ctx(
                request,
                current_user=current_user,
                error="Нужно отметить: «Я понимаю, что восстановить данные будет нельзя».",
                msg=None,
                phrase1=CONFIRM_PHRASE_1,
                phrase2=CONFIRM_PHRASE_2,
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
            _ctx(
                request,
                current_user=current_user,
                error=str(exc),
                msg=None,
                phrase1=CONFIRM_PHRASE_1,
                phrase2=CONFIRM_PHRASE_2,
            ),
            status_code=400,
        )
    return RedirectResponse(url="/admin/super/purge?msg=deleted", status_code=303)
