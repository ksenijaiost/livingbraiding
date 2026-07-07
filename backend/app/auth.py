from __future__ import annotations

"""
Auth helpers for a simple internal app.

Signed cookie session fields:
- user_id
- active_role (UserRole.value) — текущий «кабинет» при нескольких ролях
"""

from dataclasses import dataclass
from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import MasterLevel, User, UserRole
from app.db.session import get_db
from app.security import verify_password
from app.settings import get_settings
from app.user_roles import default_active_role, get_roles_for_user, resolve_active_role


@dataclass(frozen=True)
class AuthUser:
    id: int
    username: str
    display_name: str
    role: UserRole
    """Активная роль (контекст UI и require_role)."""
    roles: tuple[UserRole, ...]
    """Все назначенные роли."""
    master_level: MasterLevel | None = None


COOKIE_NAME = "lb_session"
SESSION_REMEMBER_DAYS = 30
SESSION_REMEMBER_MAX_AGE = SESSION_REMEMBER_DAYS * 24 * 60 * 60


def _serializer() -> URLSafeSerializer:
    settings = get_settings()
    return URLSafeSerializer(settings.secret_key, salt="livingbraiding-session")


def _session_token(user_id: int, active_role: UserRole, *, remember: bool = False) -> str:
    s = _serializer()
    payload: dict[str, object] = {"user_id": user_id, "active_role": active_role.value}
    if remember:
        payload["remember"] = True
    return s.dumps(payload)


def _cookie_secure() -> bool:
    return get_settings().app_env == "prod"


def _cookie_base_kwargs() -> dict[str, object]:
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": _cookie_secure(),
        "path": "/",
    }


def issue_session_cookie(
    response: Response,
    user_id: int,
    active_role: UserRole,
    *,
    remember: bool = False,
) -> None:
    token = _session_token(user_id, active_role, remember=remember)
    kwargs = dict(_cookie_base_kwargs())
    if remember:
        kwargs["max_age"] = SESSION_REMEMBER_MAX_AGE
    response.set_cookie(COOKIE_NAME, token, **kwargs)


def logout_response() -> Response:
    """Redirect to login and clear session cookie."""
    resp = Response(status_code=status.HTTP_303_SEE_OTHER)
    resp.headers["Location"] = "/login"
    resp.delete_cookie(COOKIE_NAME, **_cookie_base_kwargs())
    return resp


def canonical_staff_phone(raw: str | None) -> str | None:
    """Нормализация номера для хранения и входа: только цифры, минимум 10."""
    if raw is None or not str(raw).strip():
        return None
    digits = "".join(c for c in str(raw).strip() if c.isdigit())
    if len(digits) < 10:
        return None
    return digits[:30]


def authenticate(db: Session, login: str, password: str) -> Optional[User]:
    """Вход по логину (латиница) или по телефону (цифры, как в карточке сотрудника)."""
    raw = (login or "").strip()
    if not raw:
        return None
    login_lower = raw.lower()
    phone_key = canonical_staff_phone(raw)
    conds = [User.username == login_lower]
    if phone_key:
        conds.append(User.phone == phone_key)
    user = db.scalar(select(User).where(User.is_active.is_(True), or_(*conds)))
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def login_response(user: User, db: Session, *, remember: bool = False) -> Response:
    """Issue session cookie and redirect to `/`."""
    roles = get_roles_for_user(db, user.id)
    active = default_active_role(roles)
    resp = Response(status_code=status.HTTP_303_SEE_OTHER)
    resp.headers["Location"] = "/"
    issue_session_cookie(resp, user.id, active, remember=remember)
    return resp


def _get_session_payload(request: Request) -> tuple[Optional[int], Optional[str], bool]:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None, None, False
    s = _serializer()
    try:
        data = s.loads(token)
    except BadSignature:
        return None, None, False
    user_id = data.get("user_id")
    ar = data.get("active_role")
    remember = bool(data.get("remember"))
    if isinstance(user_id, int):
        return user_id, ar if isinstance(ar, str) else None, remember
    return None, None, False


def session_remember_from_request(request: Request) -> bool:
    _uid, _role, remember = _get_session_payload(request)
    return remember


def optional_session_user_id(request: Request) -> Optional[int]:
    """Идентификатор пользователя из подписанной сессии или None (без редиректа на /login)."""
    user_id, _, _remember = _get_session_payload(request)
    return user_id


def get_current_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> AuthUser:
    """Resolve session cookie to an AuthUser or redirect to login."""
    user_id, active_raw, _remember = _get_session_payload(request)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    roles_list = get_roles_for_user(db, user.id)
    if not roles_list:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    active = resolve_active_role(roles_list, active_raw)
    return AuthUser(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=active,
        roles=tuple(roles_list),
        master_level=user.master_level,
    )


def require_role(*roles: UserRole):
    """Dependency factory for role-based access control (по активной роли)."""

    def _dep(user: Annotated[AuthUser, Depends(get_current_user)]) -> AuthUser:
        # TECHSPEC — техническая роль: доступ ко всем страницам независимо от активной роли.
        if UserRole.TECHSPEC in user.roles:
            return user
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

    return _dep


def require_assigned_roles(*roles: UserRole):
    """Доступ, если одна из ролей назначена пользователю (в `user.roles`), не только активная."""

    allowed = frozenset(roles)

    def _dep(user: Annotated[AuthUser, Depends(get_current_user)]) -> AuthUser:
        if UserRole.TECHSPEC in user.roles:
            return user
        if not allowed.intersection(user.roles):
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

    return _dep


def require_techspec_user():
    """Только пользователи с ролью TECHSPEC (опасные операции: бэкап/восстановление медиа)."""

    def _dep(user: Annotated[AuthUser, Depends(get_current_user)]) -> AuthUser:
        if UserRole.TECHSPEC not in user.roles:
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

    return _dep


def require_admin_super_assigned():
    """Только у кого в профиле есть роль ADMIN_SUPER (без обхода для TECHSPEC)."""

    def _dep(user: Annotated[AuthUser, Depends(get_current_user)]) -> AuthUser:
        if UserRole.ADMIN_SUPER not in user.roles:
            raise HTTPException(status_code=403, detail="Только для суперадмина.")
        return user

    return _dep
