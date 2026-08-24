from __future__ import annotations


def test_master_visit_new_role_gate_allows_admin_active_role() -> None:
    from app.auth import AuthUser, require_role
    from app.db.models import MasterLevel, UserRole

    dep = require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)
    user = AuthUser(
        id=2,
        username="admin",
        display_name="Admin",
        role=UserRole.ADMIN,
        roles=(UserRole.MASTER, UserRole.ADMIN),
        master_level=MasterLevel.MIDDLE,
    )
    assert dep(user) is user


def test_master_visit_new_role_gate_allows_senior_admin_active_role() -> None:
    from app.auth import AuthUser, require_role
    from app.db.models import UserRole

    dep = require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)
    user = AuthUser(
        id=3,
        username="senior",
        display_name="Senior",
        role=UserRole.ADMIN_SENIOR,
        roles=(UserRole.ADMIN_SENIOR,),
    )
    assert dep(user) is user
