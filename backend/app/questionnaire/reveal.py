"""Условный показ блоков/полей анкеты по галочке (1.37).

Хранится в visibility_json поля анкеты (тип CHECKBOX):
{
  "reveal_on_check": {
    "blocks": ["kit", "tail", "thermo", "material_description"],
    "field_keys": ["other_field_key"]
  }
}
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from app.db.models import QuestionnaireFieldType

REVEAL_BLOCK_KIT = "kit"
REVEAL_BLOCK_TAIL = "tail"
REVEAL_BLOCK_THERMO = "thermo"
REVEAL_BLOCK_MATERIAL = "material_description"

REVEAL_BLOCKS: tuple[str, ...] = (
    REVEAL_BLOCK_KIT,
    REVEAL_BLOCK_TAIL,
    REVEAL_BLOCK_THERMO,
    REVEAL_BLOCK_MATERIAL,
)

REVEAL_BLOCK_LABELS: dict[str, str] = {
    REVEAL_BLOCK_KIT: "Раздел «Комплект» (склад)",
    REVEAL_BLOCK_TAIL: "Раздел «Хвост/резинка» (склад)",
    REVEAL_BLOCK_THERMO: "Блок «Термозамещение»",
    REVEAL_BLOCK_MATERIAL: "Поле «Описание про материал»",
}

_FIELD_KEY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,99}$")


@dataclass(frozen=True)
class RevealOnCheck:
    blocks: tuple[str, ...] = ()
    field_keys: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not self.blocks and not self.field_keys

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocks": list(self.blocks),
            "field_keys": list(self.field_keys),
        }


EMPTY_REVEAL = RevealOnCheck()


def parse_reveal_on_check(raw: str | None | dict[str, Any] | RevealOnCheck) -> RevealOnCheck:
    if isinstance(raw, RevealOnCheck):
        return raw
    data: Any = raw
    if isinstance(raw, str):
        t = raw.strip()
        if not t:
            return EMPTY_REVEAL
        try:
            data = json.loads(t)
        except json.JSONDecodeError:
            return EMPTY_REVEAL
    if not isinstance(data, dict):
        return EMPTY_REVEAL
    inner = data.get("reveal_on_check", data)
    if not isinstance(inner, dict):
        return EMPTY_REVEAL
    blocks_raw = inner.get("blocks") or []
    keys_raw = inner.get("field_keys") or inner.get("fields") or []
    blocks: list[str] = []
    if isinstance(blocks_raw, list):
        for b in blocks_raw:
            s = str(b or "").strip()
            if s in REVEAL_BLOCKS and s not in blocks:
                blocks.append(s)
    keys: list[str] = []
    if isinstance(keys_raw, list):
        for k in keys_raw:
            s = str(k or "").strip()
            if _FIELD_KEY_RE.match(s) and s not in keys:
                keys.append(s)
    if not blocks and not keys:
        return EMPTY_REVEAL
    return RevealOnCheck(blocks=tuple(blocks), field_keys=tuple(keys))


def reveal_on_check_to_visibility_json(reveal: RevealOnCheck) -> str | None:
    if reveal.is_empty():
        return None
    return json.dumps({"reveal_on_check": reveal.to_dict()}, ensure_ascii=False)


def normalize_reveal_from_form(
    *,
    field_type: QuestionnaireFieldType | str,
    reveal_blocks: Iterable[str] | None,
    reveal_field_keys_raw: str | None,
) -> tuple[RevealOnCheck, list[str]]:
    """Собрать RevealOnCheck из формы админки. Для не-CHECKBOX — пусто."""
    errors: list[str] = []
    ft = field_type if isinstance(field_type, QuestionnaireFieldType) else QuestionnaireFieldType(str(field_type).upper())
    blocks_in = [str(b or "").strip() for b in (reveal_blocks or []) if str(b or "").strip()]
    keys_raw = (reveal_field_keys_raw or "").strip()

    if ft != QuestionnaireFieldType.CHECKBOX:
        if blocks_in or keys_raw:
            errors.append("Показ блоков/полей по галочке задаётся только для типа «Галочка».")
        return EMPTY_REVEAL, errors

    blocks: list[str] = []
    for b in blocks_in:
        if b not in REVEAL_BLOCKS:
            errors.append(f"Неизвестный блок для показа: «{b}».")
            continue
        if b not in blocks:
            blocks.append(b)

    keys: list[str] = []
    if keys_raw:
        for part in re.split(r"[\s,;]+", keys_raw):
            s = part.strip()
            if not s:
                continue
            if not _FIELD_KEY_RE.match(s):
                errors.append(f"Ключ доп. поля «{s}» недопустим (латиница, цифры, _).")
                continue
            if s not in keys:
                keys.append(s)

    return RevealOnCheck(blocks=tuple(blocks), field_keys=tuple(keys)), errors


def reveal_form_prefill(visibility_json: str | None) -> dict[str, Any]:
    """Значения для чекбоксов/инпута в форме поля анкеты."""
    rev = parse_reveal_on_check(visibility_json)
    return {
        "reveal_blocks": list(rev.blocks),
        "reveal_field_keys": ", ".join(rev.field_keys),
    }


def answers_reveal_blocks(answers: dict[str, Any], specs: Iterable[Any]) -> set[str]:
    """Блоки, которые открыты отмеченными галочками с reveal_on_check."""
    out: set[str] = set()
    for spec in specs:
        if getattr(spec, "field_type", None) != QuestionnaireFieldType.CHECKBOX:
            continue
        reveal = parse_reveal_on_check(getattr(spec, "reveal_on_check", None) or getattr(spec, "visibility_json", None))
        if reveal.is_empty():
            continue
        if not bool(answers.get(spec.field_key)):
            continue
        out.update(reveal.blocks)
    return out


def answers_reveal_field_keys(answers: dict[str, Any], specs: Iterable[Any]) -> set[str]:
    out: set[str] = set()
    for spec in specs:
        if getattr(spec, "field_type", None) != QuestionnaireFieldType.CHECKBOX:
            continue
        reveal = parse_reveal_on_check(getattr(spec, "reveal_on_check", None) or getattr(spec, "visibility_json", None))
        if reveal.is_empty():
            continue
        if not bool(answers.get(spec.field_key)):
            continue
        out.update(reveal.field_keys)
    return out


def field_keys_hidden_by_default(specs: Iterable[Any]) -> set[str]:
    """Поля, которые кто-то открывает галочкой (скрыты, пока галочка не отмечена)."""
    out: set[str] = set()
    for spec in specs:
        reveal = parse_reveal_on_check(getattr(spec, "reveal_on_check", None) or getattr(spec, "visibility_json", None))
        out.update(reveal.field_keys)
        if REVEAL_BLOCK_MATERIAL in reveal.blocks:
            out.add(REVEAL_BLOCK_MATERIAL)
    return out


def specs_can_reveal_block(specs: Iterable[Any], block: str) -> bool:
    for spec in specs:
        reveal = parse_reveal_on_check(getattr(spec, "reveal_on_check", None) or getattr(spec, "visibility_json", None))
        if block in reveal.blocks:
            return True
    return False
