"""Краткоживущий flash с кодом привязки канала — без кода в URL."""

from __future__ import annotations

from typing import Any

from fastapi import Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.settings import get_settings

_COOKIE = "lb_notify_link_flash"
_MAX_AGE_SEC = 15 * 60


def _ser() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="lb-notify-link-flash")


def set_notify_link_flash(response: Response, *, channel: str, plain_code: str) -> None:
    token = _ser().dumps({"channel": channel, "code": plain_code})
    response.set_cookie(
        _COOKIE,
        token,
        max_age=_MAX_AGE_SEC,
        httponly=True,
        samesite="lax",
        secure=get_settings().app_env == "prod",
        path="/",
    )


def pop_notify_link_flash(request: Request, response: Response) -> dict[str, Any] | None:
    raw = request.cookies.get(_COOKIE)
    response.delete_cookie(_COOKIE, path="/")
    if not raw:
        return None
    try:
        data = _ser().loads(raw, max_age=_MAX_AGE_SEC)
    except BadSignature:
        return None
    if not isinstance(data, dict):
        return None
    ch = str(data.get("channel") or "").strip()
    code = str(data.get("code") or "").strip()
    if not ch or not code:
        return None
    return {"channel": ch, "code": code}
