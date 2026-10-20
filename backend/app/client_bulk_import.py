"""Массовое добавление клиентов из JSON (суперадмин)."""

from __future__ import annotations

import json
from typing import Any, TypedDict

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.client_marks import CLIENT_MARK_IMPORT_KEYS, apply_client_import_marks
from app.client_validation import (
    CLIENT_AGE_GROUP_OPTIONS,
    client_has_any_contact,
    load_client_source_options,
    parse_age_group,
    parse_birth_fields,
    parse_client_source,
    strip_or_none,
)
from app.client_phone import DuplicateClientPhone, apply_client_phone, find_client_by_normalized_phone
from app.db.models import Client
from app.phone_ru import InvalidPhoneError, format_phone, normalize_phone
from app.time_utils import utcnow_naive

MAX_BULK_JSON_BYTES = 512 * 1024
MAX_BULK_CLIENTS = 1000

BULK_CREATED_BY_LABEL = "Массовое добавление"

_ALLOWED_KEYS = frozenset(
    {
        "name",
        "phone",
        "telegram",
        "vk",
        "instagram",
        "other_contact",
        "contact_client_id",
        "contact_relation",
        "birth_day",
        "birth_month",
        "birth_year",
        "age_group",
        "source",
        "source_other",
        "comment",
        *CLIENT_MARK_IMPORT_KEYS,
    }
)

STATUS_CREATED = "создан"
STATUS_DUPLICATE = "пропущен: дубль"
STATUS_ERROR = "ошибка"


class BulkClientRowResult(TypedDict):
    line: int
    name: str
    phone: str
    status: str
    ok: bool
    client_id: int | None
    message: str


def parse_bulk_clients_json(raw: str) -> list[dict[str, Any]]:
    s = (raw or "").strip()
    if not s:
        raise ValueError("Введите JSON (массив объектов).")
    if len(s.encode("utf-8")) > MAX_BULK_JSON_BYTES:
        raise ValueError(f"Слишком большой файл (лимит {MAX_BULK_JSON_BYTES // 1024} KB).")
    try:
        data = json.loads(s)
    except json.JSONDecodeError as e:
        raise ValueError(f"Некорректный JSON: {e}") from None
    if not isinstance(data, list):
        raise ValueError("Корень JSON должен быть массивом клиентов.")
    if len(data) > MAX_BULK_CLIENTS:
        raise ValueError(f"Слишком много строк (лимит {MAX_BULK_CLIENTS}).")
    out: list[dict[str, Any]] = []
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            raise ValueError(f"Строка {i + 1}: ожидался объект, не {type(row).__name__}.")
        out.append(row)
    return out


def summarize_bulk_client_results(results: list[BulkClientRowResult]) -> dict[str, int]:
    return {
        "created": sum(1 for row in results if row["status"] == STATUS_CREATED),
        "skipped": sum(1 for row in results if row["status"] == STATUS_DUPLICATE),
        "errors": sum(1 for row in results if row["status"] == STATUS_ERROR),
    }


def _text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        raise ValueError(f"Поле «{key}» должно быть строкой.")
    return str(value)


def _birth_raw(row: dict[str, Any], key: str) -> str | None:
    if key not in row or row[key] is None or row[key] == "":
        return None
    if isinstance(row[key], bool) or isinstance(row[key], (dict, list)):
        raise ValueError("Введите целое число для даты рождения.")
    return str(row[key]).strip()


def _reject_unknown_keys(row: dict[str, Any]) -> None:
    unknown = [str(key) for key in row.keys() if key not in _ALLOWED_KEYS]
    if not unknown:
        return
    listed = ", ".join(f"«{key}»" for key in unknown)
    raise ValueError(f"Неизвестное поле: {listed}.")


def _age_group(row: dict[str, Any]):
    if "age_group" not in row or row["age_group"] in (None, ""):
        return None
    raw = _text(row, "age_group").strip()
    if not raw:
        return None
    try:
        return parse_age_group(raw)
    except ValueError as e:
        opts = ", ".join(f"{code} ({label})" for code, label in CLIENT_AGE_GROUP_OPTIONS if code)
        raise ValueError(f"{e} Допустимо: {opts}.") from e


def _source(row: dict[str, Any]) -> str | None:
    if "source" not in row:
        return None
    raw = _text(row, "source")
    try:
        return parse_client_source(raw)
    except ValueError as e:
        labels = ", ".join(item["label"] for item in load_client_source_options())
        raise ValueError(f"{e} Допустимо: {labels}.") from e


def _duplicate_row(
    *,
    line: int,
    name: str,
    phone: str,
    existing: Client | None,
) -> BulkClientRowResult:
    label = ""
    client_id = None
    if existing is not None:
        client_id = int(existing.id)
        shown = format_phone(existing.phone_normalized) or existing.phone or phone
        who = (existing.name or "").strip() or "—"
        label = f"Клиент с этим номером уже есть: {who}, {shown}"
    else:
        label = "Клиент с этим номером уже есть"
    return {
        "line": int(line),
        "name": name,
        "phone": phone,
        "status": STATUS_DUPLICATE,
        "ok": False,
        "client_id": client_id,
        "message": label,
    }


def import_single_client_row(
    db: Session,
    row: dict[str, Any],
    *,
    reserved_phones: set[str],
    changed_by_user_id: int,
    line: int = 0,
) -> BulkClientRowResult:
    """Одна строка JSON. Ошибка не откатывает уже сохранённые строки."""
    name_preview = ""
    phone_preview = ""
    try:
        raw_name = row.get("name")
        if not isinstance(raw_name, (dict, list)):
            name_preview = str(raw_name or "").strip()
        raw_phone = row.get("phone")
        if not isinstance(raw_phone, (dict, list)) and raw_phone is not None:
            phone_preview = str(raw_phone).strip()
        _reject_unknown_keys(row)
        if isinstance(raw_name, (dict, list)) or not name_preview:
            raise ValueError("Укажите имя клиента.")
        name = name_preview[:200]
        phone_raw = _text(row, "phone")
        phone_preview = phone_raw.strip()
        try:
            normalized = normalize_phone(phone_raw)
        except InvalidPhoneError as exc:
            raise ValueError(str(exc)) from exc
        telegram = strip_or_none(_text(row, "telegram"), 100)
        vk = strip_or_none(_text(row, "vk"), 120)
        instagram = strip_or_none(_text(row, "instagram"), 120)
        other_contact = strip_or_none(_text(row, "other_contact"), 200)
        if normalized:
            phone_preview = format_phone(normalized)
        contact_client_id_raw = row.get("contact_client_id")
        contact_relation_raw = row.get("contact_relation")
        if not client_has_any_contact(
            phone_raw,
            telegram,
            vk,
            instagram,
            other_contact,
            contact_client_id=contact_client_id_raw,
        ):
            raise ValueError("Нужен хотя бы один контакт: телефон, соцсеть или контактное лицо.")
        birth_day, birth_month, birth_year = parse_birth_fields(
            _birth_raw(row, "birth_day"),
            _birth_raw(row, "birth_month"),
            _birth_raw(row, "birth_year"),
        )
        age_group = _age_group(row)
        source = _source(row)
        if normalized and (normalized in reserved_phones or find_client_by_normalized_phone(db, normalized) is not None):
            return _duplicate_row(
                line=line,
                name=name,
                phone=format_phone(normalized),
                existing=find_client_by_normalized_phone(db, normalized),
            )
        client = Client(
            name=name,
            telegram=telegram,
            vk=vk,
            instagram=instagram,
            other_contact=other_contact,
            age_group=age_group,
            source=source,
            source_other=strip_or_none(_text(row, "source_other"), 200),
            comment=strip_or_none(_text(row, "comment")) or None,
            is_confirmed=True,
            birth_day=birth_day,
            birth_month=birth_month,
            birth_year=birth_year,
            created_by_label=BULK_CREATED_BY_LABEL,
            updated_by_user_id=int(changed_by_user_id),
            updated_at=utcnow_naive(),
        )
        apply_client_import_marks(client, row)
        try:
            from app.client_contact import validate_and_apply_contact

            apply_client_phone(client, phone_raw, db)
            validate_and_apply_contact(
                db,
                client,
                contact_client_id_raw=contact_client_id_raw,
                contact_relation_raw=contact_relation_raw,
                phone_normalized=client.phone_normalized,
                is_create=True,
            )
            db.add(client)
            db.flush()
            db.commit()
        except DuplicateClientPhone as exc:
            db.rollback()
            existing = find_client_by_normalized_phone(db, normalized or "")
            return _duplicate_row(
                line=line,
                name=name,
                phone=format_phone(normalized) if normalized else phone_preview,
                existing=existing,
            )
        except IntegrityError:
            db.rollback()
            existing = find_client_by_normalized_phone(db, normalized) if normalized else None
            if existing is not None:
                return _duplicate_row(
                    line=line,
                    name=name,
                    phone=format_phone(normalized) if normalized else phone_preview,
                    existing=existing,
                )
            raise
        if normalized:
            reserved_phones.add(normalized)
        return {
            "line": int(line),
            "name": name,
            "phone": format_phone(normalized) if normalized else "",
            "status": STATUS_CREATED,
            "ok": True,
            "client_id": int(client.id),
            "message": "Создан.",
        }
    except ValueError as e:
        db.rollback()
        return {
            "line": int(line),
            "name": name_preview,
            "phone": phone_preview,
            "status": STATUS_ERROR,
            "ok": False,
            "client_id": None,
            "message": str(e),
        }
    except Exception as e:
        db.rollback()
        return {
            "line": int(line),
            "name": name_preview,
            "phone": phone_preview,
            "status": STATUS_ERROR,
            "ok": False,
            "client_id": None,
            "message": f"Ошибка: {e}",
        }
