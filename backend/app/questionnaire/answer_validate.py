"""
Проверка ответов динамической анкеты (после POST) по списку MergedQuestionnaireFieldSpec.
"""

from __future__ import annotations

from typing import Any

from app.db.models import QuestionnaireFieldType
from app.questionnaire.runtime_merge import MergedQuestionnaireFieldSpec


def _parse_float(s: str) -> float:
    return float(str(s).strip().replace(",", "."))


def validate_and_coerce_answers(
    raw: dict[str, str],
    specs: list[MergedQuestionnaireFieldSpec],
) -> tuple[dict[str, Any], list[str]]:
    """
    `raw` — значения с формы без префикса q_ (ключ = field_key).
    Для чекбокса: ключ отсутствует = False; «on» / непустая строка = True.
    """
    errors: list[str] = []
    out: dict[str, Any] = {}

    for spec in specs:
        key = spec.field_key
        sval = raw.get(key)
        if sval is None:
            sval = ""
        else:
            sval = str(sval).strip()

        if spec.field_type == QuestionnaireFieldType.CHECKBOX:
            present = key in raw
            if present:
                sl = sval.lower()
                checked = sl in ("on", "1", "true", "yes", "да")
            else:
                checked = False
            if spec.required and not checked:
                errors.append(f"«{spec.label}»: отметьте галочку (обязательное поле).")
                continue
            out[key] = bool(checked)
            continue

        if spec.required and not sval:
            errors.append(f"«{spec.label}»: заполните поле.")
            continue

        if not sval and not spec.required:
            continue

        if spec.field_type == QuestionnaireFieldType.TEXT:
            out[key] = sval
        elif spec.field_type == QuestionnaireFieldType.TEXTAREA:
            out[key] = sval
        elif spec.field_type == QuestionnaireFieldType.NUMBER:
            try:
                num = _parse_float(sval)
            except ValueError:
                errors.append(f"«{spec.label}»: введите число.")
                continue
            if spec.min_value is not None and num < spec.min_value:
                errors.append(f"«{spec.label}»: не меньше {spec.min_value}.")
                continue
            if spec.max_value is not None and num > spec.max_value:
                errors.append(f"«{spec.label}»: не больше {spec.max_value}.")
                continue
            out[key] = num
        elif spec.field_type == QuestionnaireFieldType.SELECT:
            allowed = {o["value"] for o in spec.options}
            if sval not in allowed:
                errors.append(f"«{spec.label}»: выберите значение из списка.")
                continue
            out[key] = sval
        else:
            errors.append(f"«{spec.label}»: неподдерживаемый тип поля.")

    return out, errors


def extract_questionnaire_raw_from_form(form: Any) -> dict[str, str]:
    """Собрать q_<field_key> → строковое значение (для передачи в validate)."""
    from starlette.datastructures import UploadFile

    out: dict[str, str] = {}
    for k in form.keys():
        if not isinstance(k, str) or not k.startswith("q_"):
            continue
        fk = k[2:]
        if not fk:
            continue
        v = form.get(k)
        if isinstance(v, UploadFile):
            continue
        if isinstance(v, (bytes, bytearray)):
            out[fk] = v.decode().strip()
        else:
            out[fk] = str(v).strip() if v is not None else ""
    return out


def extract_line_questionnaire_raw_from_form(form: Any, idx: int) -> dict[str, str]:
    """Собрать line_<idx>_q_<field_key> → {field_key: value}."""
    from starlette.datastructures import UploadFile

    prefix = f"line_{int(idx)}_q_"
    out: dict[str, str] = {}
    for k in form.keys():
        if not isinstance(k, str) or not k.startswith(prefix):
            continue
        fk = k[len(prefix) :]
        if not fk:
            continue
        vs = [v for v in form.getlist(k) if not isinstance(v, UploadFile)]
        if not vs:
            continue
        v = vs[-1]
        out[fk] = v.decode().strip() if isinstance(v, (bytes, bytearray)) else str(v).strip() if v is not None else ""
    return out
