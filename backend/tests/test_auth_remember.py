from __future__ import annotations

from starlette.responses import Response

from app.auth import (
    SESSION_REMEMBER_MAX_AGE,
    _get_session_payload,
    issue_session_cookie,
    session_remember_from_request,
)
from app.db.models import UserRole
from starlette.requests import Request


def _response_cookie_header(resp: Response) -> str:
    raw = resp.raw_headers
    for name, value in raw:
        if name.lower() == b"set-cookie":
            return value.decode()
    return ""


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

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"cookie", f"lb_session={token}".encode())],
    }
    request = Request(scope)
    uid, role, remember = _get_session_payload(request)
    assert uid == 42
    assert role == UserRole.ADMIN.value
    assert remember is True
    assert session_remember_from_request(request) is True
