from __future__ import annotations

import time

from starlette.responses import Response
from starlette.requests import Request

from app.auth import (
    SESSION_REMEMBER_MAX_AGE,
    SESSION_REMEMBER_RENEW_AFTER,
    _get_session_payload,
    _session_token,
    issue_session_cookie,
    remember_session_needs_renew,
    renew_remember_session_cookie_if_needed,
    session_remember_from_request,
)
from app.db.models import UserRole
from app.settings import get_settings
from itsdangerous import URLSafeSerializer


def _response_cookie_header(resp: Response) -> str:
    raw = resp.raw_headers
    for name, value in raw:
        if name.lower() == b"set-cookie":
            return value.decode()
    return ""


def _request_with_cookie(token: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"cookie", f"lb_session={token}".encode())],
    }
    return Request(scope)


def test_issue_session_cookie_remember_sets_max_age() -> None:
    resp = Response()
    issue_session_cookie(resp, 7, UserRole.MASTER, remember=True)
    header = _response_cookie_header(resp)
    assert "lb_session=" in header
    assert f"Max-Age={SESSION_REMEMBER_MAX_AGE}" in header
    assert "HttpOnly" in header


def test_issue_session_cookie_without_remember_is_session_cookie() -> None:
    resp = Response()
    issue_session_cookie(resp, 7, UserRole.MASTER, remember=False)
    header = _response_cookie_header(resp)
    assert "lb_session=" in header
    assert "Max-Age=" not in header


def test_session_remember_roundtrip_in_token() -> None:
    resp = Response()
    issue_session_cookie(resp, 42, UserRole.ADMIN, remember=True)
    header = _response_cookie_header(resp)
    token = header.split("lb_session=", 1)[1].split(";", 1)[0]

    request = _request_with_cookie(token)
    uid, role, remember = _get_session_payload(request)
    assert uid == 42
    assert role == UserRole.ADMIN.value
    assert remember is True
    assert session_remember_from_request(request) is True


def test_remember_token_includes_iat() -> None:
    token = _session_token(1, UserRole.MASTER, remember=True)
    s = URLSafeSerializer(get_settings().secret_key, salt="livingbraiding-session")
    data = s.loads(token)
    assert data.get("remember") is True
    assert isinstance(data.get("iat"), int)


def test_remember_session_needs_renew_without_iat() -> None:
    s = URLSafeSerializer(get_settings().secret_key, salt="livingbraiding-session")
    token = s.dumps({"user_id": 5, "active_role": UserRole.MASTER.value, "remember": True})
    request = _request_with_cookie(token)
    assert remember_session_needs_renew(request) is True


def test_remember_session_renew_when_iat_old() -> None:
    s = URLSafeSerializer(get_settings().secret_key, salt="livingbraiding-session")
    old_iat = int(time.time()) - SESSION_REMEMBER_RENEW_AFTER - 10
    token = s.dumps(
        {
            "user_id": 9,
            "active_role": UserRole.ADMIN.value,
            "remember": True,
            "iat": old_iat,
        }
    )
    request = _request_with_cookie(token)
    assert remember_session_needs_renew(request) is True
    resp = Response()
    assert renew_remember_session_cookie_if_needed(request, resp) is True
    header = _response_cookie_header(resp)
    assert "lb_session=" in header
    assert f"Max-Age={SESSION_REMEMBER_MAX_AGE}" in header


def test_remember_session_no_renew_when_fresh() -> None:
    resp = Response()
    issue_session_cookie(resp, 3, UserRole.MASTER, remember=True)
    token = _response_cookie_header(resp).split("lb_session=", 1)[1].split(";", 1)[0]
    request = _request_with_cookie(token)
    assert remember_session_needs_renew(request) is False
    out = Response()
    assert renew_remember_session_cookie_if_needed(request, out) is False
    assert _response_cookie_header(out) == ""
