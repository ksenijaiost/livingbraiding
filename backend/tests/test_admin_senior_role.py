from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient

from app.auth import AuthUser, get_current_user, require_role
from app.db.models import UserRole
from app.main import app
from app.role_access import (
    role_can_edit_catalog,
    role_gets_home_dashboard,
    role_is_admin_staff,
    role_is_master_schedule_admin,
)


def _senior_user() -> AuthUser:
    return AuthUser(
        id=10,
        username="senior",
        display_name="Senior Admin",
        role=UserRole.ADMIN_SENIOR,
        roles=(UserRole.ADMIN_SENIOR,),
    )


def _plain_admin_user() -> AuthUser:
    return AuthUser(
        id=11,
        username="admin",
        display_name="Admin",
        role=UserRole.ADMIN,
        roles=(UserRole.ADMIN,),
    )


def test_require_role_senior_inherits_admin() -> None:
    dep = require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)
    user = _senior_user()
    assert dep(user) == user


def test_require_role_senior_denied_super_only() -> None:
    dep = require_role(UserRole.ADMIN_SUPER)
    with pytest.raises(HTTPException) as exc:
        dep(_senior_user())
    assert exc.value.status_code == 403


def test_role_access_helpers() -> None:
    assert role_is_admin_staff(UserRole.ADMIN_SENIOR)
    assert role_can_edit_catalog(UserRole.ADMIN_SENIOR)
    assert role_is_master_schedule_admin(UserRole.ADMIN_SENIOR)
    assert not role_can_edit_catalog(UserRole.ADMIN)
    assert role_gets_home_dashboard(UserRole.ADMIN_SENIOR)
    assert role_gets_home_dashboard(UserRole.ADMIN)
    assert not role_gets_home_dashboard(UserRole.TECHSPEC)


def test_senior_admin_can_open_expenses() -> None:
    app.dependency_overrides[get_current_user] = _senior_user
    try:
        client = TestClient(app)
        assert client.get("/admin/expenses", follow_redirects=False).status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_senior_admin_catalog_route_allowed() -> None:
    dep_role = require_role(UserRole.ADMIN_SENIOR, UserRole.ADMIN_SUPER)
    assert dep_role(_senior_user()) == _senior_user()


def test_plain_admin_forbidden_on_expenses_and_catalog() -> None:
    app.dependency_overrides[get_current_user] = _plain_admin_user
    try:
        client = TestClient(app)
        assert client.get("/admin/expenses", follow_redirects=False).status_code == 403
    finally:
        app.dependency_overrides.clear()
    dep_role = require_role(UserRole.ADMIN_SENIOR, UserRole.ADMIN_SUPER)
    with pytest.raises(HTTPException):
        dep_role(_plain_admin_user())


def test_ru_label_for_senior_admin() -> None:
    from app.ru_labels import ru_user_role

    assert ru_user_role(UserRole.ADMIN_SENIOR) == "Старший админ"


def test_admin_catalog_template_has_role_helpers() -> None:
    """admin_service_catalog must use shared webui templates (base.html needs role_* globals)."""
    from starlette.requests import Request

    from app.webui import ctx, templates
    import app.admin_service_catalog as catalog_mod

    assert catalog_mod.templates is templates
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/admin/catalog",
        "headers": [],
        "query_string": b"",
    }
    html = templates.get_template("admin_catalog_index.html").render(
        ctx(
            Request(scope),
            current_user=_senior_user(),
            cat_rows=[],
            total_services=0,
            err=None,
        )
    )
    assert "Прайс" in html or "категор" in html.lower() or "Новая" in html
