"""Обязательные фото при закрытии визита: сбор URL из формы и проверка."""

from __future__ import annotations

from typing import Any

from app.forms_parse import parse_bool
from app.media_store import get_nonempty_upload, note_media_for_release, save_upload_image

VISIT_PHOTO_FIELDS = ("photo_1", "photo_2", "photo_3")

VISIT_PHOTO_REQUIRED_MSG = "Загрузи фото клиента. Если не сделала, кидай селфи 🙂"
VISIT_PHOTO_KEEP_ONE_MSG = "У визита должно остаться хотя бы одно фото"

_KEPT_PREFIXES = ("kept_", "pending_")


def _form_get(form: Any, name: str) -> Any:
    if hasattr(form, "get"):
        return form.get(name)
    return None


def _kept_url_from_form(form: Any, field: str) -> str | None:
    for prefix in _KEPT_PREFIXES:
        raw = _form_get(form, f"{prefix}{field}")
        if raw is None:
            continue
        s = str(raw).strip()
        if s.startswith("/media/"):
            return s
    return None


def photo_tuple_has_any(photos: tuple[str | None, str | None, str | None] | list[str | None]) -> bool:
    return any(bool((p or "").strip()) for p in photos)


def require_at_least_one_photo(photos: tuple[str | None, str | None, str | None]) -> None:
    if not photo_tuple_has_any(photos):
        raise ValueError(VISIT_PHOTO_REQUIRED_MSG)


async def resolve_visit_photo_slot(
    form: Any,
    field: str,
    current: str | None,
    *,
    db: object | None = None,
) -> tuple[str | None, bool]:
    """Снять / заменить / оставить слот. Учитывает kept_/pending_ при потере file input."""
    url = (current or None) or None
    changed = False
    if parse_bool(_form_get(form, f"clear_{field}")):
        note_media_for_release(db, url)
        url = None
        changed = True
    upload = get_nonempty_upload(form, field)
    if upload is not None:
        new_url = await save_upload_image(upload)
        if url:
            note_media_for_release(db, url)
        url = new_url
        changed = True
    elif not changed:
        kept = _kept_url_from_form(form, field)
        if kept and kept != (current or None):
            # Сохранённое с прошлой неудачной отправки: подставляем, если слот пуст.
            if not url:
                url = kept
                changed = True
        elif kept and not url:
            url = kept
    return url, changed


async def resolve_visit_photos(
    form: Any,
    *,
    current: tuple[str | None, str | None, str | None] = (None, None, None),
    db: object | None = None,
) -> tuple[tuple[str | None, str | None, str | None], bool]:
    """Вернуть (photo_1, photo_2, photo_3) и флаг «что-то менялось»."""
    out: list[str | None] = []
    any_changed = False
    for i, field in enumerate(VISIT_PHOTO_FIELDS):
        cur = current[i] if i < len(current) else None
        url, did = await resolve_visit_photo_slot(form, field, cur, db=db)
        out.append(url)
        if did:
            any_changed = True
    return (out[0], out[1], out[2]), any_changed


async def save_uploaded_visit_photos_for_prefill(form: Any) -> dict[str, str]:
    """При ошибке валидации: сохранить выбранные файлы и вернуть URL для prefill/kept_."""
    fp: dict[str, str] = {}
    for field in VISIT_PHOTO_FIELDS:
        if parse_bool(_form_get(form, f"clear_{field}")):
            fp[f"clear_{field}"] = "1"
            continue
        upload = get_nonempty_upload(form, field)
        if upload is not None:
            try:
                url = await save_upload_image(upload)
            except ValueError:
                # Битый файл — не подставляем; пользователь увидит основную ошибку / выберет снова.
                continue
            fp[field] = url
            fp[f"kept_{field}"] = url
            continue
        kept = _kept_url_from_form(form, field)
        if kept:
            fp[field] = kept
            fp[f"kept_{field}"] = kept
    return fp


def apply_photos_to_prefill(fp: dict[str, str], photos: tuple[str | None, str | None, str | None]) -> None:
    for i, field in enumerate(VISIT_PHOTO_FIELDS):
        url = photos[i] if i < len(photos) else None
        if url:
            fp[field] = url
            fp[f"kept_{field}"] = url
        else:
            fp.pop(field, None)
            fp.pop(f"kept_{field}", None)


def draft_photos_tuple(draft: Any) -> tuple[str | None, str | None, str | None]:
    return (
        getattr(draft, "photo_1", None) or None,
        getattr(draft, "photo_2", None) or None,
        getattr(draft, "photo_3", None) or None,
    )


def assign_photos(target: Any, photos: tuple[str | None, str | None, str | None]) -> None:
    target.photo_1 = photos[0]
    target.photo_2 = photos[1]
    target.photo_3 = photos[2]
