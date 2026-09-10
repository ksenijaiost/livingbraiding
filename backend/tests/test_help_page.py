"""Страница /help (шаг 2.2)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.auth import AuthUser, get_current_user
from app.db.models import UserRole
from app.main import app


def _master_user() -> AuthUser:
    return AuthUser(
        id=1,
        username="master1",
        display_name="Мастер",
        role=UserRole.MASTER,
        roles=(UserRole.MASTER,),
    )


def test_help_requires_auth() -> None:
    client = TestClient(app)
    r = client.get("/help", follow_redirects=False)
    assert r.status_code in (303, 307, 401, 403)
    if r.status_code in (303, 307):
        assert "/login" in (r.headers.get("location") or "")


def test_help_faq_page_for_master() -> None:
    app.dependency_overrides[get_current_user] = _master_user
    try:
        client = TestClient(app)
        r = client.get("/help", follow_redirects=False)
        assert r.status_code == 200
        assert "Справка" in r.text
        assert "мастер" in r.text.lower()
        assert 'href="/help"' in r.text
    finally:
        app.dependency_overrides.clear()
