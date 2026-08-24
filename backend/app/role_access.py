"""Проверки доступа по ролям (старший админ и наследование прав админа)."""

from __future__ import annotations

from app.db.models import UserRole

ROLES_ADMIN_STAFF = frozenset({UserRole.ADMIN, UserRole.ADMIN_SENIOR, UserRole.ADMIN_SUPER})
ROLES_CATALOG_EDITOR = frozenset({UserRole.ADMIN_SENIOR, UserRole.ADMIN_SUPER})
ROLES_MASTER_SCHEDULE_ADMIN = frozenset({UserRole.ADMIN_SENIOR, UserRole.ADMIN_SUPER})


def role_is_admin_staff(role: UserRole) -> bool:
    return role in ROLES_ADMIN_STAFF


def role_can_edit_catalog(role: UserRole) -> bool:
    return role in ROLES_CATALOG_EDITOR


def role_is_master_schedule_admin(role: UserRole) -> bool:
    return role in ROLES_MASTER_SCHEDULE_ADMIN


def role_is_admin_super(role: UserRole) -> bool:
    return role == UserRole.ADMIN_SUPER


def active_role_matches(user_role: UserRole, allowed: frozenset[UserRole]) -> bool:
    if user_role in allowed:
        return True
    if user_role == UserRole.ADMIN_SENIOR and UserRole.ADMIN in allowed:
        return True
    return False
