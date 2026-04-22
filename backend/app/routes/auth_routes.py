from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import (
    AuthUser,
    authenticate,
    get_current_user,
    issue_session_cookie,
    login_response,
    logout_response,
)
from app.db.models import UserRole
from app.db.session import get_db
from app.webui import ctx as _ctx, templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html",
        _ctx(request, current_user=None, error=None),
    )


@router.get("/login/forgot", response_class=HTMLResponse)
def login_forgot_page(request: Request):
    return templates.TemplateResponse(
        "login_forgot.html",
        _ctx(request, current_user=None),
    )


@router.post("/login")
def login_action(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    """Cookie-based login. On success, redirects to `/`."""
    user = authenticate(db, login=username.strip(), password=password)
    if not user:
        return templates.TemplateResponse(
            "login.html",
            _ctx(
                request,
                current_user=None,
                error="Неверный логин / телефон или пароль.",
            ),
            status_code=400,
        )
    return login_response(user, db)


@router.post("/session/active-role")
def session_set_active_role(
    request: Request,
    role: str = Form(...),
    current_user: AuthUser = Depends(get_current_user),
):
    try:
        new_role = UserRole(role.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="Некорректная роль")
    if new_role not in current_user.roles:
        raise HTTPException(status_code=403, detail="Эта роль не назначена пользователю")
    # Всегда на главную: прежний URL может быть только для другой роли (403).
    resp = RedirectResponse(url="/", status_code=303)
    issue_session_cookie(resp, current_user.id, new_role)
    return resp


@router.get("/logout")
def logout_action():
    return logout_response()
