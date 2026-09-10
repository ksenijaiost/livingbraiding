"""Страница /help и кнопка «?» (шаги 2.2–2.3)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.auth import AuthUser, get_current_user
from app.db.models import UserRole
from app.help_docs import clear_help_cache, get_page_help
from app.main import app
from app.webui import ctx as build_ctx


def setup_function() -> None:
    clear_help_cache()


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
        # Кнопка «?» и модалка (page-help help_faq)
        assert 'id="lbPageHelpOpen"' in r.text
        assert 'id="lb-page-help-modal"' in r.text
        assert "Справка по странице" in r.text
        assert "Открыть общую справку" in r.text
    finally:
        app.dependency_overrides.clear()


def test_ctx_autoloads_page_help_doc() -> None:
    assert get_page_help("home", UserRole.MASTER) is not None
    out = build_ctx(
        request=None,  # type: ignore[arg-type]
        current_user=_master_user(),
        help_page_id="home",
        display_tz="UTC",
    )
    assert out["page_help_doc"] is not None
    assert out["page_help_doc"].title == "Главная"
