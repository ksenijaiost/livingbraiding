"""
Валидация полей анкеты перед сохранением (описание поля + JSON опций для SELECT).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.db.models import QuestionnaireFieldType

FIELD_KEY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,99}$")


@dataclass(frozen=True)
class NormalizedQuestionnaireField:
    """Нормализованные данные для записи в БД / сериализации."""

    field_key: str
    field_type: QuestionnaireFieldType
    label: str
    required: bool
    placeholder: str | None
    help_text: str | None
    options_json: str | None
    min_value: float | None
    max_value: float | None

    def as_structure_dict(self) -> dict[str, Any]:
        """Каноническое JSON-подобное описание поля после валидации."""
        d: dict[str, Any] = {
            "field_key": self.field_key,
            "field_type": self.field_type.value,
            "label": self.label,
            "required": self.required,
            "placeholder": self.placeholder,
            "help_text": self.help_text,
        }
        if self.options_json is not None:
            d["options"] = json.loads(self.options_json)
        if self.min_value is not None:
            d["min_value"] = self.min_value
        if self.max_value is not None:
            d["max_value"] = self.max_value
        return d


def _strip_or_none(s: str | None, max_len: int | None = None) -> str | None:
    if s is None:
        return None
    t = str(s).strip()
    if not t:
        return None
    if max_len is not None and len(t) > max_len:
        return t[:max_len]
    return t


def _parse_optional_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    t = str(raw).strip()
    if not t:
        return None
    return float(t.replace(",", "."))


def _normalize_select_options(raw: str) -> tuple[str | None, list[str]]:
    """
    Парсит и нормализует options для SELECT.
    Возвращает (json_string, errors). При ошибках первый элемент None.
    """
    t = raw.strip()
    if not t:
        return None, ["Для типа «выбор из списка» задайте варианты (JSON)."]
    try:
        data = json.loads(t)
    except json.JSONDecodeError as e:
        return None, [f"Опции: невалидный JSON ({e.msg})."]

    if not isinstance(data, list) or len(data) == 0:
        return None, ["Опции: нужен непустой JSON-массив."]

    normalized: list[dict[str, str]] = []
    for i, item in enumerate(data):
        if isinstance(item, str):
            s = item.strip()
            if not s:
                return None, [f"Опции: элемент {i + 1} — пустая строка недопустима."]
            normalized.append({"value": s, "label": s})
        elif isinstance(item, dict):
            v = item.get("value")
            lbl = item.get("label")
            if not isinstance(v, str) or not isinstance(lbl, str):
                return None, [f"Опции: элемент {i + 1} — ожидается строка или объект с полями value и label (строки)."]
            v, lbl = v.strip(), lbl.strip()
            if not v or not lbl:
                return None, [f"Опции: элемент {i + 1} — value и label не могут быть пустыми."]
            normalized.append({"value": v, "label": lbl})
        else:
            return None, [f"Опции: элемент {i + 1} — недопустимый тип."]

    return json.dumps(normalized, ensure_ascii=False), []


def validate_questionnaire_field_form(
    *,
    field_key: str,
    field_type_raw: str,
    label: str,
    required: bool,
    placeholder: str | None,
    help_text: str | None,
    options_raw: str | None,
    min_raw: str | None,
    max_raw: str | None,
    edit_field_key_locked: str | None = None,
) -> tuple[NormalizedQuestionnaireField | None, list[str]]:
    """
    Валидация входа с формы суперадмина.

    Если `edit_field_key_locked` задан (режим правки), `field_key` в запросе должен совпадать,
    иначе в ответе будет ошибка (ключ поля в ответах мастеров менять нельзя).
    """
    errors: list[str] = []

    key = (field_key or "").strip()
    if edit_field_key_locked is not None:
        if key != edit_field_key_locked:
            errors.append("Идентификатор поля (ключ) нельзя менять после создания.")
        key = edit_field_key_locked
    else:
        if not key:
            errors.append("Укажите ключ поля (латиница, цифры, _, с буквы).")
        elif not FIELD_KEY_RE.match(key):
            errors.append(
                "Ключ: 1–100 символов, начинается с буквы, допустимы латиница, цифры и символ _."
            )

    label_n = _strip_or_none(label, 500)
    if not label_n:
        errors.append("Укажите подпись поля (на русском).")

    try:
        ft = QuestionnaireFieldType(str(field_type_raw or "").strip().upper())
    except ValueError:
        return None, ["Неизвестный тип поля."]

    ph = _strip_or_none(placeholder, 500)
    ht = _strip_or_none(help_text, None)

    options_json: str | None = None
    min_v: float | None = None
    max_v: float | None = None

    if ft == QuestionnaireFieldType.SELECT:
        opt_s, opt_err = _normalize_select_options((options_raw or "").strip() or "[]")
        if opt_err:
            errors.extend(opt_err)
        else:
            options_json = opt_s
    else:
        t = (options_raw or "").strip()
        if t:
            errors.append("Поле «варианты (JSON)» допустимо только для типа «выбор из списка».")

    if ft == QuestionnaireFieldType.NUMBER:
        min_v = None
        max_v = None
        number_parse_ok = True
        try:
            min_v = _parse_optional_float(min_raw)
            max_v = _parse_optional_float(max_raw)
        except ValueError:
            number_parse_ok = False
            errors.append("Мин./макс.: введите число или оставьте пустым.")
        if (
            number_parse_ok
            and min_v is not None
            and max_v is not None
            and min_v > max_v
        ):
            errors.append("Минимум не может быть больше максимума.")
    else:
        if (min_raw or "").strip() or (max_raw or "").strip():
            errors.append("Границы min/max задаются только для типа «число».")

    if errors:
        return None, errors

    assert label_n is not None
    return (
        NormalizedQuestionnaireField(
            field_key=key,
            field_type=ft,
            label=label_n,
            required=required,
            placeholder=ph,
            help_text=ht,
            options_json=options_json,
            min_value=min_v,
            max_value=max_v,
        ),
        [],
    )
