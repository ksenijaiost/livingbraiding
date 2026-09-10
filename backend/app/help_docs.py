"""Загрузка встроенной справки из файлов help_content/."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import bleach
import markdown
import yaml

from app.db.models import UserRole

HELP_CONTENT_DIR = Path(__file__).resolve().parent / "help_content"
FAQ_DIR = HELP_CONTENT_DIR / "faq"
PAGES_DIR = HELP_CONTENT_DIR / "pages"
MANIFEST_PATH = HELP_CONTENT_DIR / "manifest.yaml"

_PAGE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)

_MD_EXTENSIONS = [
    "markdown.extensions.fenced_code",
    "markdown.extensions.tables",
    "markdown.extensions.nl2br",
    "markdown.extensions.sane_lists",
]

_ALLOWED_TAGS = [
    "p",
    "br",
    "strong",
    "em",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "a",
    "code",
    "pre",
    "blockquote",
    "hr",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
]
_ALLOWED_ATTRIBUTES = {
    "a": ["href", "title", "rel"],
    "code": ["class"],
}
_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


@dataclass(frozen=True)
class HelpDocument:
    """Распарсенный документ справки."""

    title: str
    body_md: str
    body_html: str
    roles: frozenset[str] | None  # None = без ограничения по ролям в front-matter
    source_path: str
    page_id: str | None = None


def role_slug(role: UserRole | str) -> str:
    """ADMIN_SUPER → admin_super."""
    if isinstance(role, UserRole):
        return role.value.lower()
    return str(role).strip().lower()


def is_valid_page_id(page_id: str) -> bool:
    return bool(page_id) and bool(_PAGE_ID_RE.fullmatch(page_id))


def clear_help_cache() -> None:
    """Сброс кэша (для тестов)."""
    _load_markdown_file.cache_clear()
    _load_manifest.cache_clear()


def get_faq(role: UserRole) -> HelpDocument | None:
    """FAQ кабинета для роли. Нет файла → None."""
    path = FAQ_DIR / f"{role_slug(role)}.md"
    doc = _load_markdown_file(str(path))
    if doc is None:
        return None
    if not _role_allowed(doc.roles, role):
        return None
    return doc


def get_page_help(page_id: str, role: UserRole) -> HelpDocument | None:
    """Справка страницы для роли. Нет файла / роль не подходит / битый id → None."""
    if not is_valid_page_id(page_id):
        return None
    path = PAGES_DIR / f"{page_id}.md"
    doc = _load_markdown_file(str(path), page_id=page_id)
    if doc is None:
        return None
    if not _role_allowed(doc.roles, role):
        return None
    return doc


def has_page_help(page_id: str, role: UserRole) -> bool:
    return get_page_help(page_id, role) is not None


def render_markdown_safe(text: str) -> str:
    """Markdown → HTML с санитизацией (без XSS из сырого HTML/js-ссылок)."""
    raw = markdown.markdown(text or "", extensions=_MD_EXTENSIONS, output_format="html")
    cleaned = bleach.clean(
        raw,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )
    return bleach.linkify(cleaned, parse_email=False, skip_tags=["pre", "code"])


def parse_front_matter(raw: str) -> tuple[dict[str, Any], str]:
    """Вернуть (meta, body_md). Без front-matter meta пустой."""
    text = raw.lstrip("\ufeff")
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return {}, text
    meta_raw, body = m.group(1), m.group(2)
    try:
        meta = yaml.safe_load(meta_raw) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(meta, dict):
        return {}, text
    return meta, body


def _role_allowed(roles: frozenset[str] | None, role: UserRole) -> bool:
    if roles is None:
        return True
    return role_slug(role) in roles


def _normalize_roles(value: Any) -> frozenset[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return None
    out = {str(x).strip().lower() for x in items if str(x).strip()}
    return frozenset(out) if out else None


@lru_cache(maxsize=256)
def _load_markdown_file(path_str: str, page_id: str | None = None) -> HelpDocument | None:
    path = Path(path_str)
    if not path.is_file():
        return None
    # Защита от выхода за пределы help_content
    try:
        path.resolve().relative_to(HELP_CONTENT_DIR.resolve())
    except ValueError:
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, body_md = parse_front_matter(raw)
    title = str(meta.get("title") or "").strip() or _default_title(path, page_id)
    roles = _normalize_roles(meta.get("roles"))
    body_html = render_markdown_safe(body_md)
    return HelpDocument(
        title=title,
        body_md=body_md,
        body_html=body_html,
        roles=roles,
        source_path=str(path.relative_to(HELP_CONTENT_DIR.parent)),
        page_id=page_id,
    )


@lru_cache(maxsize=1)
def _load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.is_file():
        return {"pages": {}}
    try:
        data = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {"pages": {}}
    if not isinstance(data, dict):
        return {"pages": {}}
    pages = data.get("pages") or {}
    if not isinstance(pages, dict):
        pages = {}
    return {"pages": pages}


def get_manifest_pages() -> dict[str, Any]:
    """Сырой реестр pages из manifest.yaml (может быть пустым)."""
    return dict(_load_manifest().get("pages") or {})


def _default_title(path: Path, page_id: str | None) -> str:
    if page_id:
        return page_id.replace("_", " ").strip().capitalize() or path.stem
    return path.stem.replace("_", " ").strip().capitalize() or "Справка"
