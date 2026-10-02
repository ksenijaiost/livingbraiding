"""Загрузчик встроенной справки (шаг 2.1)."""

from __future__ import annotations

from app.db.models import UserRole
from app.help_docs import (
    clear_help_cache,
    get_faq,
    get_page_help,
    has_page_help,
    is_valid_page_id,
    parse_front_matter,
    render_markdown_safe,
    role_slug,
    strip_leading_atx_h1,
)


def setup_function() -> None:
    clear_help_cache()


def test_role_slug() -> None:
    assert role_slug(UserRole.ADMIN_SUPER) == "admin_super"
    assert role_slug(UserRole.MASTER) == "master"
    assert role_slug("ADMIN_SENIOR") == "admin_senior"


def test_is_valid_page_id() -> None:
    assert is_valid_page_id("admin_clients_list")
    assert not is_valid_page_id("")
    assert not is_valid_page_id("../etc/passwd")
    assert not is_valid_page_id("Admin")
    assert not is_valid_page_id("_example")


def test_parse_front_matter() -> None:
    meta, body = parse_front_matter(
        "---\ntitle: Клиенты\nroles: [admin, master]\n---\n\n# Hello\n"
    )
    assert meta["title"] == "Клиенты"
    assert meta["roles"] == ["admin", "master"]
    assert body.strip().startswith("# Hello")


def test_parse_front_matter_absent() -> None:
    meta, body = parse_front_matter("# Only body\n")
    assert meta == {}
    assert body.startswith("# Only body")


def test_render_markdown_strips_raw_html_and_js() -> None:
    html = render_markdown_safe('Hello <script>alert(1)</script> **bold**')
    assert "<script>" not in html
    assert "<strong>bold</strong>" in html or "<strong>bold</strong>" in html.lower()
    assert "Hello" in html

    linked = render_markdown_safe("[x](javascript:alert(1))")
    assert "javascript:" not in linked.lower()


def test_get_faq_master_stub() -> None:
    doc = get_faq(UserRole.MASTER)
    assert doc is not None
    assert "мастер" in doc.title.lower() or "Мастер" in doc.title
    assert doc.body_html
    assert doc.roles is None or "master" in doc.roles


def test_get_faq_all_roles_have_files() -> None:
    for role in UserRole:
        doc = get_faq(role)
        assert doc is not None, f"missing FAQ for {role}"
        assert "будет заполнен" not in doc.body_md
        assert "шаге 5" not in doc.body_md
        assert "Что умеет кабинет" in doc.body_md
        assert "Важные ограничения" in doc.body_md


def test_get_page_help_example_not_valid_id() -> None:
    # _example.md специально с подчёркиванием — не валидный page_id
    assert get_page_help("_example", UserRole.ADMIN) is None
    assert has_page_help("missing_page", UserRole.ADMIN) is False


def test_get_page_help_rejects_path_traversal() -> None:
    assert get_page_help("../faq/master", UserRole.MASTER) is None
    assert get_page_help("foo/bar", UserRole.MASTER) is None


def test_page_help_strips_leading_h1_faq_keeps_it() -> None:
    page = get_page_help("clients_list", UserRole.MASTER)
    assert page is not None
    assert "<h1>" not in page.body_html.lower()
    assert "Что делать здесь" in page.body_html
    faq = get_faq(UserRole.MASTER)
    assert faq is not None
    assert "<h1>" in faq.body_html.lower()


def test_admin_senior_inherits_admin_page_help_roles() -> None:
    """Как active_role_matches: senior видит материалы с roles: [admin]."""
    from app.help_docs import HELP_CONTENT_DIR

    path = HELP_CONTENT_DIR / "pages" / "admin_only_tmp.md"
    path.write_text(
        "---\ntitle: Only admin\nroles: [admin]\n---\n\n# Only admin\n\nBody.\n",
        encoding="utf-8",
    )
    try:
        clear_help_cache()
        assert get_page_help("admin_only_tmp", UserRole.ADMIN) is not None
        assert get_page_help("admin_only_tmp", UserRole.ADMIN_SENIOR) is not None
        assert get_page_help("admin_only_tmp", UserRole.MASTER) is None
    finally:
        path.unlink(missing_ok=True)
        clear_help_cache()


def test_strip_leading_atx_h1() -> None:
    assert strip_leading_atx_h1("# Title\n\nBody\n").startswith("Body")
    assert strip_leading_atx_h1("## Not h1\n").startswith("## Not h1")


def test_external_links_get_noopener() -> None:
    html = render_markdown_safe("[x](https://example.com/a)")
    assert 'rel="noopener noreferrer"' in html
    assert "https://example.com/a" in html
