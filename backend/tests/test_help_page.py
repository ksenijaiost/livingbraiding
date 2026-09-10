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


def test_mvp_page_help_files_load_for_roles() -> None:
    """Ключевые экраны шага 2.6: файл есть и роли из front-matter совпадают."""
    cases: list[tuple[str, UserRole]] = [
        ("clients_list", UserRole.MASTER),
        ("visits_list", UserRole.ADMIN),
        ("master_visit_form", UserRole.MASTER),
        ("kits_list", UserRole.ADMIN_SENIOR),
        ("product_sales_list", UserRole.MASTER),
        ("bookings_list", UserRole.ADMIN),
        ("studio_expenses", UserRole.ADMIN_SENIOR),
        ("payroll_periods", UserRole.ADMIN_SUPER),
        ("payroll_fund", UserRole.ADMIN_SUPER),
        ("operational_report", UserRole.ADMIN_SUPER),
        ("admin_settings", UserRole.ADMIN_SUPER),
    ]
    for page_id, role in cases:
        doc = get_page_help(page_id, role)
        assert doc is not None, f"{page_id} for {role}"
        assert doc.body_html
    # Обычный админ не видит расходы старшего
    assert get_page_help("studio_expenses", UserRole.ADMIN) is None
    assert get_page_help("payroll_periods", UserRole.MASTER) is None


def test_help_page_id_block_does_not_leak_into_body() -> None:
    """{% block help_page_id %} не должен печатать id видимым текстом в начале страницы."""
    from pathlib import Path

    from starlette.requests import Request

    from app.webui import templates

    probe = Path(__file__).resolve().parents[1] / "app" / "templates" / "_help_block_probe.html"
    probe.write_text(
        "{% extends \"base.html\" %}\n"
        "{% block help_page_id %}clients_list{% endblock %}\n"
        "{% block content %}<div id=\"probe-ok\">OK</div>{% endblock %}\n",
        encoding="utf-8",
    )
    try:
        html = templates.get_template("_help_block_probe.html").render(
            {
                "request": Request(
                    {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
                ),
                "current_user": _master_user(),
                "title": "probe",
                "display_tz": "UTC",
            }
        )
    finally:
        probe.unlink(missing_ok=True)

    assert 'id="probe-ok"' in html
    assert 'id="lbPageHelpOpen"' in html
    brand_at = html.find("Живем Плетем")
    assert brand_at > 0
    assert "clients_list" not in html[:brand_at]
