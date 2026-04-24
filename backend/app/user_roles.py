"""Множественные роли пользователя + выбор активной роли в сессии."""

from __future__ import annotations

from sqlalchemy import Select, delete, select
from sqlalchemy.orm import Session

from app.db.models import User, UserRole, UserRoleAssignment

_ROLE_PRIORITY = (
    UserRole.TECHSPEC,
    UserRole.ADMIN_SUPER,
    UserRole.ADMIN,
    UserRole.MASTER,
)


def max_user_role(roles: list[UserRole]) -> UserRole:
    order = {r: i for i, r in enumerate(_ROLE_PRIORITY)}
    return max(roles, key=lambda r: order[r])


def default_active_role(roles: list[UserRole]) -> UserRole:
    """Начальный контекст после входа: приоритет админских ролей."""
    if UserRole.TECHSPEC in roles:
        return UserRole.TECHSPEC
    if UserRole.ADMIN_SUPER in roles:
        return UserRole.ADMIN_SUPER
    if UserRole.ADMIN in roles:
        return UserRole.ADMIN
    return UserRole.MASTER


def get_roles_for_user(db: Session, user_id: int) -> list[UserRole]:
    rows = list(
        db.scalars(
            select(UserRoleAssignment.role).where(UserRoleAssignment.user_id == user_id)
        ).all()
    )
    if not rows:
        return []
    order = {UserRole.TECHSPEC: -1, UserRole.ADMIN_SUPER: 0, UserRole.ADMIN: 1, UserRole.MASTER: 2}
    return sorted(set(rows), key=lambda r: order[r])


def resolve_active_role(roles: list[UserRole], cookie_value: str | None) -> UserRole:
    if cookie_value:
        try:
            r = UserRole(cookie_value)
            if r in roles:
                return r
        except ValueError:
            pass
    return default_active_role(roles)


def user_has_role(db: Session, user_id: int, role: UserRole) -> bool:
    return role in set(get_roles_for_user(db, user_id))


def user_has_any_role(db: Session, user_id: int, *roles: UserRole) -> bool:
    return bool(set(get_roles_for_user(db, user_id)).intersection(set(roles)))


def sync_user_denormalized_role(db: Session, user_id: int) -> None:
    """Колонка users.role — максимальная из назначенных (удобно для legacy/отображения)."""
    u = db.get(User, user_id)
    if not u:
        return
    roles = get_roles_for_user(db, user_id)
    if roles:
        u.role = max_user_role(roles)


def set_user_roles(db: Session, user: User, roles: list[UserRole]) -> None:
    """Полная замена ролей пользователя (минимум одна)."""
    if not roles:
        raise ValueError("Нужна хотя бы одна роль.")
    seen: set[UserRole] = set()
    uniq: list[UserRole] = []
    for r in roles:
        if r in seen:
            continue
        seen.add(r)
        uniq.append(r)
    db.execute(delete(UserRoleAssignment).where(UserRoleAssignment.user_id == user.id))
    for r in uniq:
        db.add(UserRoleAssignment(user_id=user.id, role=r))
    user.role = max_user_role(uniq)


def select_users_with_role(role: UserRole) -> Select[tuple[User]]:
    """Запрос User с назначенной ролью role."""
    return (
        select(User)
        .join(UserRoleAssignment, UserRoleAssignment.user_id == User.id)
        .where(User.is_active.is_(True), UserRoleAssignment.role == role)
        .distinct()
    )


def select_users_with_any_role(*roles: UserRole) -> Select[tuple[User]]:
    return (
        select(User)
        .join(UserRoleAssignment, UserRoleAssignment.user_id == User.id)
        .where(User.is_active.is_(True), UserRoleAssignment.role.in_(roles))
        .distinct()
    )
