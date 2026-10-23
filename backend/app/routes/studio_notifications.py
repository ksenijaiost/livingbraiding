"""Студия → управление общими чатами и внутренними уведомлениями."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.admin_chat import (
    CHAT_KIND_ADMIN,
    CHAT_KIND_GENERAL,
    admin_chats_for_ui,
    chat_test_message,
    create_admin_chat_link_token,
    get_admin_chat_target,
    normalize_chat_kind,
    send_to_chat,
    unlink_admin_chat,
)
from app.auth import AuthUser, get_current_user, require_admin_super_assigned
from app.db.session import get_db
from app.internal_notify_prefs import events_for_ui, save_internal_notify_prefs
from app.role_access import role_is_admin_super
from app.webui import ctx as _ctx, templates

router = APIRouter(prefix="/studio", tags=["studio-notifications"])

_SUPER = Depends(require_admin_super_assigned())
_CODE_COOKIE = "lb_studio_chat_code"
_KIND_COOKIE = "lb_studio_chat_kind"


def _chat_blocks(db: Session, *, issued_kind: str | None, issued_code: str | None) -> list[dict]:
    specs = (
        (
            CHAT_KIND_ADMIN,
            "Чат админов",
            "/admins",
            "Добавьте бота в чат и отправьте /admins КОД. Код действует 24 часа.",
        ),
        (
            CHAT_KIND_GENERAL,
            "Общий чат",
            "/general",
            "Добавьте бота в чат и отправьте /general КОД. Код действует 24 часа.",
        ),
    )
    blocks = []
    for kind, title, command, hint in specs:
        blocks.append(
            {
                "kind": kind,
                "title": title,
                "command": command,
                "hint": hint,
                "code": issued_code if issued_kind == kind else None,
                "channels": admin_chats_for_ui(db, chat_kind=kind),
            }
        )
    return blocks


@router.get("/notifications", response_class=HTMLResponse)
def studio_notifications_page(
    request: Request,
    msg: str | None = None,
    err: str | None = None,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    info = {
        "code": "Код создан (24 часа). Отправьте его в нужный чат.",
        "saved": "Настройки внутренних уведомлений сохранены.",
        "unlinked": "Чат отключён.",
        "test_ok": "Тестовое сообщение отправлено.",
    }.get(msg or "")
    error = {
        "kind": "Неизвестный вид чата.",
        "missing": "Чат для этого канала не подключён.",
        "test_fail": "Не удалось отправить тест.",
        "unlink_fail": "Не удалось отключить чат.",
    }.get(err or "")
    issued_code = request.cookies.get(_CODE_COOKIE)
    issued_kind = request.cookies.get(_KIND_COOKIE)
    resp = templates.TemplateResponse(
        "studio_notifications.html",
        _ctx(
            request,
            current_user=current_user,
            can_edit=role_is_admin_super(current_user.role),
            chat_blocks=_chat_blocks(db, issued_kind=issued_kind, issued_code=issued_code),
            notify_events=events_for_ui(db),
            info=info,
            error=error,
            help_page_id="studio_notifications",
        ),
    )
    if issued_code:
        resp.delete_cookie(_CODE_COOKIE, path="/")
        resp.delete_cookie(_KIND_COOKIE, path="/")
    return resp


def _redirect(msg: str | None = None, err: str | None = None) -> RedirectResponse:
    if err:
        return RedirectResponse(url=f"/studio/notifications?err={err}", status_code=303)
    if msg:
        return RedirectResponse(url=f"/studio/notifications?msg={msg}", status_code=303)
    return RedirectResponse(url="/studio/notifications", status_code=303)


@router.post("/notifications/chat/code")
async def studio_notifications_chat_code(
    request: Request,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    form = await request.form()
    try:
        kind = normalize_chat_kind(str(form.get("chat_kind") or ""))
    except ValueError:
        return _redirect(err="kind")
    plain = create_admin_chat_link_token(db, created_by_user_id=current_user.id, chat_kind=kind)
    db.commit()
    resp = _redirect(msg="code")
    resp.set_cookie(_CODE_COOKIE, plain, max_age=15 * 60, httponly=True, samesite="lax", path="/")
    resp.set_cookie(_KIND_COOKIE, kind, max_age=15 * 60, httponly=True, samesite="lax", path="/")
    return resp


@router.post("/notifications/chat/test")
async def studio_notifications_chat_test(
    request: Request,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    form = await request.form()
    try:
        kind = normalize_chat_kind(str(form.get("chat_kind") or ""))
    except ValueError:
        return _redirect(err="kind")
    channel = str(form.get("channel") or "").strip().lower()
    row = get_admin_chat_target(db, channel, chat_kind=kind)
    if row is None:
        return _redirect(err="missing")
    try:
        send_to_chat(channel, int(row.chat_id), chat_test_message(kind))
    except Exception:
        return _redirect(err="test_fail")
    return _redirect(msg="test_ok")


@router.post("/notifications/chat/unlink")
async def studio_notifications_chat_unlink(
    request: Request,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    form = await request.form()
    try:
        kind = normalize_chat_kind(str(form.get("chat_kind") or ""))
    except ValueError:
        return _redirect(err="kind")
    channel = str(form.get("channel") or "").strip().lower()
    try:
        unlink_admin_chat(db, channel, notify=True, chat_kind=kind)
        db.commit()
    except Exception:
        db.rollback()
        return _redirect(err="unlink_fail")
    return _redirect(msg="unlinked")


@router.post("/notifications/settings/save")
async def studio_notifications_settings_save(
    request: Request,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    form = await request.form()
    save_internal_notify_prefs(db, form, user_id=current_user.id)
    db.commit()
    return _redirect(msg="saved")
