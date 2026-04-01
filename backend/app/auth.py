from __future__ import annotations

"""
Auth helpers for a simple internal app.

We use a signed cookie session (itsdangerous serializer) with fields:
- user_id

This is intentionally minimal for MVP:
- no refresh tokens
- no OAuth
- role checks are enforced per-route via dependencies
"""

from dataclasses import dataclass
from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import User, UserRole
from app.db.session import get_db
from app.security import verify_password
from app.settings import get_settings


@dataclass(frozen=True)
class AuthUser:
    id: int
    username: str
    display_name: str
    role: UserRole


COOKIE_NAME = "lb_session"


def _serializer() -> URLSafeSerializer:
    settings = get_settings()
    return URLSafeSerializer(settings.secret_key, salt="livingbraiding-session")


def _issue_cookie(response: Response, user: User) -> None:
    s = _serializer()
    token = s.dumps({"user_id": user.id})
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
    )


def logout_response() -> Response:
    """Redirect to login and clear session cookie."""
    resp = Response(status_code=status.HTTP_303_SEE_OTHER)
    resp.headers["Location"] = "/login"
    resp.delete_cookie(COOKIE_NAME)
    return resp


def authenticate(db: Session, username: str, password: str) -> Optional[User]:
    """Return a User if credentials are valid, otherwise None."""
    user = db.scalar(select(User).where(User.username == username, User.is_active.is_(True)))
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def login_response(user: User) -> Response:
    """Issue session cookie and redirect to `/`."""
    resp = Response(status_code=status.HTTP_303_SEE_OTHER)
    resp.headers["Location"] = "/"
    _issue_cookie(resp, user)
    return resp


def _get_current_user_id(request: Request) -> Optional[int]:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    s = _serializer()
    try:
        data = s.loads(token)
    except BadSignature:
        return None
    user_id = data.get("user_id")
    if isinstance(user_id, int):
        return user_id
    return None


def get_current_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> AuthUser:
    """Resolve session cookie to an AuthUser or redirect to login."""
    user_id = _get_current_user_id(request)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return AuthUser(id=user.id, username=user.username, display_name=user.display_name, role=user.role)


def require_role(*roles: UserRole):
    """Dependency factory for role-based access control."""
    def _dep(user: Annotated[AuthUser, Depends(get_current_user)]) -> AuthUser:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

    return _dep

