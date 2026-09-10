"""Встроенная справка: FAQ кабинета по active_role."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth import AuthUser, get_current_user
from app.help_docs import get_faq
from app.ru_labels import ru_user_role
from app.webui import ctx as _ctx
from app.webui import templates

router = APIRouter(tags=["help"])


@router.get("/help", response_class=HTMLResponse)
def help_faq_page(
    request: Request,
    current_user: AuthUser = Depends(get_current_user),
):
    """Обзор системы для текущего кабинета (active_role)."""
    doc = get_faq(current_user.role)
    role_label = ru_user_role(current_user.role)
    return templates.TemplateResponse(
        "help/faq.html",
        _ctx(
            request,
            current_user=current_user,
            title=f"Справка — {role_label}",
            help_doc=doc,
            role_label=role_label,
            help_page_id="help_faq",
        ),
    )
