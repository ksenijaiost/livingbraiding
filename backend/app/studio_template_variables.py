"""Переменные студии для шаблонов клиентских уведомлений.

Встроенные ключи создаёт миграция; ensure_builtin_studio_variables добивает пропуски.
Удалить встроенную строку нельзя — только очистить значение или видео.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import StudioTemplateVariable
from app.media_store import VIDEO_MIME, delete_media_file, media_root_dir, save_upload_video
from app.settings import get_settings
from app.time_utils import utcnow_naive

KIND_TEXT = "text"
KIND_URL = "url"
KIND_VIDEO = "video"

# Системные переменные шаблона «до записи» — свои ключи с ними не пересекаются.
SYSTEM_TEMPLATE_KEYS = frozenset(
    {"date", "time", "datetime", "date_text", "weekday", "name", "services", "masters"}
)

SYSTEM_TEMPLATE_PALETTE: tuple[tuple[str, str], ...] = (
    ("date", "дата ДД.ММ.ГГГГ"),
    ("time", "время ЧЧ:ММ"),
    ("datetime", "ДД.ММ.ГГГГ ЧЧ:ММ"),
    ("date_text", "дата словами, например 16 сентября"),
    ("weekday", "день недели"),
    ("name", "имя клиента"),
    ("services", "услуги с новой строки и маркером"),
    ("masters", "мастера через запятую"),
)

# key, название в UI, kind
BUILTIN_SPECS: tuple[tuple[str, str, str], ...] = (
    ("address", "Адрес студии", KIND_TEXT),
    ("route_video", "Видео «Как нас найти»", KIND_VIDEO),
    ("review_yandex", "Ссылка на отзывы Яндекс Карты", KIND_URL),
    ("review_2gis", "Ссылка на отзывы 2ГИС", KIND_URL),
    ("booking_link", "Ссылка для записи", KIND_URL),
    ("studio_vk", "Сообщество VK", KIND_URL),
    ("studio_telegram", "Аккаунт студии в Telegram", KIND_URL),
    ("studio_max", "Аккаунт студии в Max", KIND_URL),
)

BUILTIN_KEYS = frozenset(spec[0] for spec in BUILTIN_SPECS)
BUILTIN_TITLES = {spec[0]: spec[1] for spec in BUILTIN_SPECS}
BUILTIN_KINDS = {spec[0]: spec[2] for spec in BUILTIN_SPECS}
ROUTE_VIDEO_KEY = "route_video"

_KEY_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_MAX_TEXT = 8000


def copy_token(key: str) -> str:
    return "{{" + key + "}}"


def ensure_builtin_studio_variables(db: Session) -> bool:
    """Создать недостающие встроенные строки. Существующие не трогает."""
    have = set(db.scalars(select(StudioTemplateVariable.key)).all())
    created = False
    now = utcnow_naive()
    for key, _title, kind in BUILTIN_SPECS:
        if key in have:
            continue
        db.add(
            StudioTemplateVariable(
                key=key,
                value=None,
                kind=kind,
                is_builtin=True,
                updated_at=now,
            )
        )
        created = True
    if created:
        db.flush()
    return created


def list_variables(db: Session) -> list[StudioTemplateVariable]:
    ensure_builtin_studio_variables(db)
    rows = list(db.scalars(select(StudioTemplateVariable).order_by(StudioTemplateVariable.id.asc())).all())
    order = {key: i for i, (key, _t, _k) in enumerate(BUILTIN_SPECS)}
    rows.sort(key=lambda r: (0 if r.is_builtin else 1, order.get(r.key, 100), r.key))
    return rows


def studio_template_values(db: Session) -> dict[str, str]:
    """Ключ → подстановка. Пустое значение — пустая строка. Видео — публичная ссылка или ''."""
    rows = list(db.scalars(select(StudioTemplateVariable)).all())
    out: dict[str, str] = {}
    for row in rows:
        key = (row.key or "").strip()
        if not key:
            continue
        if row.kind == KIND_VIDEO:
            out[key] = public_video_url(row.public_token) if row.public_token and row.file_path else ""
        else:
            out[key] = row.value or ""
    return out


def public_video_url(token: str | None) -> str:
    raw = (token or "").strip()
    if not raw:
        return ""
    path = f"/media/v/{raw}"
    base = (get_settings().public_base_url or "").rstrip("/")
    if base:
        return f"{base}{path}"
    return path


def variables_palette(db: Session) -> list[dict[str, str]]:
    """Системные, встроенные и свои — для подсказки со «скопировать»."""
    items: list[dict[str, str]] = []
    for key, label in SYSTEM_TEMPLATE_PALETTE:
        items.append({"group": "system", "key": key, "label": label, "copy": copy_token(key)})
    custom: list[StudioTemplateVariable] = []
    by_key = {r.key: r for r in list_variables(db)}
    for key, title, _kind in BUILTIN_SPECS:
        items.append({"group": "studio", "key": key, "label": title, "copy": copy_token(key)})
    for row in by_key.values():
        if not row.is_builtin:
            custom.append(row)
    for row in sorted(custom, key=lambda r: r.key):
        items.append(
            {
                "group": "custom",
                "key": row.key,
                "label": "своя переменная",
                "copy": copy_token(row.key),
            }
        )
    return items


def variables_for_ui(db: Session) -> dict[str, object]:
    rows = list_variables(db)
    by_key = {r.key: r for r in rows}
    builtins: list[dict[str, object]] = []
    for key, title, kind in BUILTIN_SPECS:
        row = by_key.get(key)
        builtins.append(
            {
                "key": key,
                "title": title,
                "kind": kind,
                "value": (row.value or "") if row else "",
                "file_name": (row.file_name or "") if row else "",
                "file_size": int(row.file_size or 0) if row else 0,
                "has_video": bool(row and row.kind == KIND_VIDEO and row.public_token and row.file_path),
                "video_url": public_video_url(row.public_token) if row and row.public_token and row.file_path else "",
                "copy": copy_token(key),
            }
        )
    custom = [
        {"key": r.key, "value": r.value or "", "copy": copy_token(r.key)}
        for r in rows
        if not r.is_builtin
    ]
    return {"builtins": builtins, "custom": custom, "palette": variables_palette(db)}


def normalize_custom_key(raw: str) -> str:
    s = (raw or "").strip()
    if not _KEY_RE.fullmatch(s):
        raise ValueError("Ключ: латиница, цифры и символ _, от 2 до 32 символов.")
    key = s.lower()
    if key in SYSTEM_TEMPLATE_KEYS:
        raise ValueError(f"Ключ «{key}» занят системной переменной (дата, имя, услуги и т.п.).")
    if key in BUILTIN_KEYS:
        raise ValueError(f"Ключ «{key}» — встроенная переменная студии, её нельзя создать заново.")
    return key


def _clean_text(raw: str) -> str | None:
    s = (raw or "").strip()
    if len(s) > _MAX_TEXT:
        raise ValueError(f"Текст слишком длинный (максимум {_MAX_TEXT} символов).")
    return s or None


def _clean_url(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    if not (s.startswith("https://") or s.startswith("http://")):
        raise ValueError("Ссылка должна начинаться с http:// или https://.")
    if len(s) > 2000:
        raise ValueError("Ссылка слишком длинная.")
    return s


def save_builtin_values(db: Session, values: dict[str, str], *, user_id: int | None) -> None:
    """Текстовые и URL-встроенные. Видео этим методом не меняется."""
    ensure_builtin_studio_variables(db)
    now = utcnow_naive()
    by_key = {r.key: r for r in db.scalars(select(StudioTemplateVariable)).all()}
    for key, kind in BUILTIN_KINDS.items():
        if kind == KIND_VIDEO or key not in values:
            continue
        row = by_key.get(key)
        if row is None or not row.is_builtin:
            continue
        raw = values[key]
        row.value = _clean_url(raw) if kind == KIND_URL else _clean_text(raw)
        row.updated_at = now
        row.updated_by_user_id = user_id
    db.flush()


def add_custom_variable(db: Session, key_raw: str, value_raw: str, *, user_id: int | None) -> StudioTemplateVariable:
    key = normalize_custom_key(key_raw)
    exists = db.scalar(select(StudioTemplateVariable.id).where(StudioTemplateVariable.key == key).limit(1))
    if exists is not None:
        raise ValueError(f"Переменная «{key}» уже есть.")
    row = StudioTemplateVariable(
        key=key,
        value=_clean_text(value_raw),
        kind=KIND_TEXT,
        is_builtin=False,
        updated_at=utcnow_naive(),
        updated_by_user_id=user_id,
    )
    db.add(row)
    db.flush()
    return row


def update_custom_variable(db: Session, key_raw: str, value_raw: str, *, user_id: int | None) -> None:
    key = (key_raw or "").strip().lower()
    row = db.scalar(select(StudioTemplateVariable).where(StudioTemplateVariable.key == key).limit(1))
    if row is None or row.is_builtin:
        raise ValueError("Своя переменная не найдена.")
    row.value = _clean_text(value_raw)
    row.updated_at = utcnow_naive()
    row.updated_by_user_id = user_id
    db.flush()


def delete_variable(db: Session, key_raw: str) -> None:
    key = (key_raw or "").strip().lower()
    row = db.scalar(select(StudioTemplateVariable).where(StudioTemplateVariable.key == key).limit(1))
    if row is None:
        raise ValueError("Переменная не найдена.")
    if row.is_builtin:
        raise ValueError("Встроенную переменную нельзя удалить — можно только очистить значение.")
    db.delete(row)
    db.flush()


def _route_video_row(db: Session) -> StudioTemplateVariable:
    ensure_builtin_studio_variables(db)
    row = db.scalar(
        select(StudioTemplateVariable).where(StudioTemplateVariable.key == ROUTE_VIDEO_KEY).limit(1)
    )
    if row is None:
        raise ValueError("Переменная видео не найдена.")
    return row


def _new_token(db: Session) -> str:
    for _ in range(5):
        token = secrets.token_urlsafe(32)[:64]
        if not _TOKEN_RE.fullmatch(token):
            continue
        taken = db.scalar(
            select(StudioTemplateVariable.id).where(StudioTemplateVariable.public_token == token).limit(1)
        )
        if taken is None:
            return token
    raise ValueError("Не удалось выдать ссылку на видео. Повторите загрузку.")


async def save_route_video(db: Session, upload: object, *, user_id: int | None) -> None:
    settings = get_settings()
    max_bytes = int(settings.studio_route_video_max_mb) * 1024 * 1024
    rel_name, original, size = await save_upload_video(upload, max_bytes=max_bytes)
    row = _route_video_row(db)
    old = row.file_path
    if not row.public_token:
        row.public_token = _new_token(db)
    row.file_path = rel_name
    row.file_name = original
    row.file_size = int(size)
    row.kind = KIND_VIDEO
    row.value = None
    row.updated_at = utcnow_naive()
    row.updated_by_user_id = user_id
    db.flush()
    if old and old != rel_name:
        delete_media_file(old)


def clear_route_video(db: Session, *, user_id: int | None) -> None:
    row = _route_video_row(db)
    old = row.file_path
    row.file_path = None
    row.file_name = None
    row.file_size = None
    row.public_token = None
    row.value = None
    row.updated_at = utcnow_naive()
    row.updated_by_user_id = user_id
    db.flush()
    delete_media_file(old)


def resolve_public_video(db: Session, token: str) -> tuple[Path, str] | None:
    raw = (token or "").strip()
    if not _TOKEN_RE.fullmatch(raw):
        return None
    row = db.scalar(
        select(StudioTemplateVariable).where(StudioTemplateVariable.public_token == raw).limit(1)
    )
    if row is None or row.kind != KIND_VIDEO or not row.file_path:
        return None
    name = Path(row.file_path).name
    if not name or name != row.file_path or ".." in name:
        return None
    root = media_root_dir().resolve()
    path = (root / name).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if not path.is_file():
        return None
    mime = VIDEO_MIME.get(path.suffix.lower())
    if not mime:
        return None
    return path, mime
