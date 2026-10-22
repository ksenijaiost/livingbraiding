"""Переменные студии для шаблонов клиентских уведомлений.

Встроенные ключи создаёт миграция; ensure_builtin_studio_variables добивает пропуски.
Удалить встроенную строку нельзя — только очистить значение или видео.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import StudioTemplateVariable
from app.media_store import note_media_for_release, save_upload_video
from app.settings import get_settings
from app.time_utils import utcnow_naive

KIND_TEXT = "text"
KIND_URL = "url"
KIND_VIDEO = "video"

# Системные переменные шаблона «до записи» — свои ключи с ними не пересекаются.
SYSTEM_TEMPLATE_KEYS = frozenset(
    {
        "date",
        "time",
        "datetime",
        "date_text",
        "weekday",
        "name",
        "services",
        "services_with_duration",
        "masters",
        "reply_hint",
        "reply_hint_no_confirm",
        "reply_hint_general",
        "client_for_line",
    }
)

SYSTEM_TEMPLATE_PALETTE: tuple[tuple[str, str], ...] = (
    ("date", "дата ДД.ММ.ГГГГ"),
    ("time", "время ЧЧ:ММ"),
    ("datetime", "ДД.ММ.ГГГГ ЧЧ:ММ"),
    ("date_text", "дата словами, например 16 сентября"),
    ("weekday", "день недели"),
    ("name", "имя клиента"),
    ("services", "услуги с новой строки и маркером, без длительности"),
    ("services_with_duration", "то же, с длительностью"),
    ("masters", "мастера через запятую"),
    ("reply_hint", "подсказка с цифрами ответов (из настроек)"),
    ("reply_hint_no_confirm", "то же без «подтвердить»"),
    ("reply_hint_general", "подсказка только: написать администратору, как добраться"),
    ("client_for_line", "«Запись для: имя» при отправке через контактное лицо"),
)

# key, название в UI, kind
BUILTIN_SPECS: tuple[tuple[str, str, str], ...] = (
    ("address", "Адрес студии", KIND_TEXT),
    ("route_video", "Видео «Как нас найти»", KIND_VIDEO),
    ("review_yandex", "Ссылка на отзывы Яндекс Карты", KIND_URL),
    ("review_2gis", "Ссылка на отзывы 2ГИС", KIND_URL),
    ("studio_vk", "Сообщество VK", KIND_URL),
    ("studio_telegram", "Аккаунт студии в Telegram", KIND_URL),
    ("studio_max", "Аккаунт студии в Max", KIND_URL),
)

BUILTIN_KEYS = frozenset(spec[0] for spec in BUILTIN_SPECS)
BUILTIN_TITLES = {spec[0]: spec[1] for spec in BUILTIN_SPECS}
BUILTIN_KINDS = {spec[0]: spec[2] for spec in BUILTIN_SPECS}
ROUTE_VIDEO_KEY = "route_video"

_KEY_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")
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
    """Ключ → подстановка. Пустое значение — пустая строка. Видео в текст не подставляется."""
    rows = list(db.scalars(select(StudioTemplateVariable)).all())
    out: dict[str, str] = {}
    for row in rows:
        key = (row.key or "").strip()
        if not key or row.kind == KIND_VIDEO:
            continue
        out[key] = row.value or ""
    return out


def _staff_video_url(row: StudioTemplateVariable | None) -> str:
    """Превью на странице уведомлений для вошедшего сотрудника."""
    if row is None or row.kind != KIND_VIDEO or not row.file_path:
        return ""
    name = Path(str(row.file_path)).name
    if not name or name != row.file_path or ".." in name:
        return ""
    return f"/media/{name}"


def variables_palette(db: Session) -> list[dict[str, str]]:
    """Системные, встроенные и свои — для подсказки со «скопировать»."""
    items: list[dict[str, str]] = []
    for key, label in SYSTEM_TEMPLATE_PALETTE:
        items.append({"group": "system", "key": key, "label": label, "token": copy_token(key)})
    custom: list[StudioTemplateVariable] = []
    by_key = {r.key: r for r in list_variables(db)}
    for key, title, _kind in BUILTIN_SPECS:
        items.append({"group": "studio", "key": key, "label": title, "token": copy_token(key)})
    for row in by_key.values():
        if not row.is_builtin:
            custom.append(row)
    for row in sorted(custom, key=lambda r: r.key):
        items.append(
            {
                "group": "custom",
                "key": row.key,
                "label": "своя переменная",
                "token": copy_token(row.key),
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
                "has_video": bool(_staff_video_url(row)),
                "video_url": _staff_video_url(row),
                "token": copy_token(key),
            }
        )
    custom = [
        {"key": r.key, "value": r.value or "", "token": copy_token(r.key)}
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


def _clear_route_video_channel_cache(row: StudioTemplateVariable) -> None:
    row.vk_video_attachment = None
    row.max_video_token = None
    row.telegram_video_file_id = None
    row.public_token = None


async def save_route_video(db: Session, upload: object, *, user_id: int | None) -> None:
    settings = get_settings()
    max_bytes = int(settings.studio_route_video_max_mb) * 1024 * 1024
    rel_name, original, size = await save_upload_video(upload, max_bytes=max_bytes)
    row = _route_video_row(db)
    old = row.file_path
    _clear_route_video_channel_cache(row)
    row.file_path = rel_name
    row.file_name = original
    row.file_size = int(size)
    row.kind = KIND_VIDEO
    row.value = None
    row.updated_at = utcnow_naive()
    row.updated_by_user_id = user_id
    db.flush()
    if old and old != rel_name:
        note_media_for_release(db, old)


def clear_route_video(db: Session, *, user_id: int | None) -> None:
    row = _route_video_row(db)
    old = row.file_path
    row.file_path = None
    row.file_name = None
    row.file_size = None
    row.value = None
    _clear_route_video_channel_cache(row)
    row.updated_at = utcnow_naive()
    row.updated_by_user_id = user_id
    db.flush()
    note_media_for_release(db, old)
