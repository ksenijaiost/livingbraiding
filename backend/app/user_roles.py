"""Множественные роли пользователя + выбор активной роли в сессии."""

from __future__ import annotations

from sqlalchemy import Select, delete, select
from sqlalchemy.orm import Session

from app.db.models import User, UserRole, UserRoleAssignment

_ROLE_PRIORITY = (
    UserRole.TECHSPEC,
    UserRole.ADMIN_SUPER,
    UserRole.ADMIN_SENIOR,
    UserRole.ADMIN,
    UserRole.MASTER,
    UserRole.HELPER,
)

_ROLE_SORT_ORDER = {
    UserRole.TECHSPEC: -1,
    UserRole.ADMIN_SUPER: 0,
    UserRole.ADMIN_SENIOR: 1,
    UserRole.ADMIN: 2,
    UserRole.MASTER: 3,
    UserRole.HELPER: 4,
}


def max_user_role(roles: list[UserRole]) -> UserRole:
    order = {r: i for i, r in enumerate(_ROLE_PRIORITY)}
    return max(roles, key=lambda r: order[r])


def default_active_role(roles: list[UserRole]) -> UserRole:
    """Начальный контекст после входа: приоритет админских ролей."""
    if UserRole.ADMIN_SUPER in roles:
        return UserRole.ADMIN_SUPER
    if UserRole.ADMIN_SENIOR in roles:
        return UserRole.ADMIN_SENIOR
    if UserRole.ADMIN in roles:
        return UserRole.ADMIN
    if UserRole.MASTER in roles:
        return UserRole.MASTER
    if UserRole.HELPER in roles:
        return UserRole.HELPER
    return UserRole.TECHSPEC


def get_roles_for_user(db: Session, user_id: int) -> list[UserRole]:
    rows = list(
        db.scalars(
            select(UserRoleAssignment.role).where(UserRoleAssignment.user_id == user_id)
        ).all()
    )
    if not rows:
        return []
    return sorted(set(rows), key=lambda r: _ROLE_SORT_ORDER.get(r, 99))


def roles_from_loaded_user(user: User) -> list[UserRole]:
    """Роли из уже загруженного relationship role_assignments (без доп. запроса)."""
    rows = [a.role for a in (user.role_assignments or []) if a.role is not None]
    if not rows and user.role is not None:
        rows = [user.role]
    return sorted(set(rows), key=lambda r: _ROLE_SORT_ORDER.get(r, 99))


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
        .where(
            User.is_active.is_(True),
            UserRoleAssignment.role == role,
            # TECHSPEC — не сотрудник: не показываем в подборках/списках по ролям.
            User.id.not_in(
                select(UserRoleAssignment.user_id).where(UserRoleAssignment.role == UserRole.TECHSPEC)
            ),
        )
        .distinct()
    )


def select_users_with_any_role(*roles: UserRole) -> Select[tuple[User]]:
    return (
        select(User)
        .join(UserRoleAssignment, UserRoleAssignment.user_id == User.id)
        .where(
            User.is_active.is_(True),
            UserRoleAssignment.role.in_(roles),
            # TECHSPEC — не сотрудник: не показываем в подборках/списках по ролям.
            User.id.not_in(
                select(UserRoleAssignment.user_id).where(UserRoleAssignment.role == UserRole.TECHSPEC)
            ),
        )
        .distinct()
    )


# Сотрудники для фондов / статистики (не техспец).
PAYROLL_STAFF_ROLES: tuple[UserRole, ...] = (
    UserRole.MASTER,
    UserRole.HELPER,
    UserRole.ADMIN,
    UserRole.ADMIN_SENIOR,
    UserRole.ADMIN_SUPER,
)


def staff_list_group(roles: list[UserRole]) -> int:
    """Порядок в списках: 0 мастера → 1 помощники → 2 админы."""
    s = set(roles)
    if UserRole.MASTER in s:
        return 0
    if UserRole.HELPER in s:
        return 1
    return 2


def list_payroll_staff_users(db: Session) -> list[User]:
    """Активные сотрудники (мастер/помощник/админы), без техспеца; сортировка: мастера, помощники, админы, имя."""
    users = list(
        db.scalars(select_users_with_any_role(*PAYROLL_STAFF_ROLES).order_by(User.display_name.asc())).all()
    )
    roles_by_uid = {int(u.id): get_roles_for_user(db, int(u.id)) for u in users}
    users.sort(
        key=lambda u: (
            staff_list_group(roles_by_uid.get(int(u.id), [])),
            (u.display_name or "").casefold(),
            int(u.id),
        )
    )
    return users
