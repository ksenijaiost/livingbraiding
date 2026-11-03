"""Уровни амортизации: справочник в настройках и снимок суммы в визите."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import FieldChange, diff_fields, write_audit_rows
from app.db.models import AmortizationLevel, AmortizationLevelAuditLog, AmortizationLevelOption
from app.forms_parse import parse_float
from app.time_utils import utcnow_naive

logger = logging.getLogger(__name__)

DEFAULT_LEVELS: tuple[tuple[str, str, float, str | None], ...] = (
    (AmortizationLevel.MIN.value, "Минимальная", 100.0, None),
    (AmortizationLevel.MID.value, "Средняя", 200.0, "включая украшения, резинки и пр."),
    (AmortizationLevel.MAX.value, "Увеличенная", 500.0, "включая средства для волос и пр."),
)

MAX_NAME_LEN = 120
MAX_DESCRIPTION_LEN = 2000
MAX_CODE_LEN = 32


@dataclass(frozen=True)
class AmortizationChoice:
    code: str
    name: str
    amount: float
    description: str | None = None
    deleted: bool = False


def ensure_default_amortization_levels(db: Session) -> bool:
    """Пустая таблица — три стартовых уровня. Иначе False."""
    if db.scalar(select(AmortizationLevelOption.id).limit(1)) is not None:
        return False
    now = utcnow_naive()
    for position, (code, name, amount, description) in enumerate(DEFAULT_LEVELS):
        db.add(
            AmortizationLevelOption(
                code=code,
                name=name,
                amount=float(amount),
                description=description,
                position=position,
                created_at=now,
            )
        )
    db.flush()
    return True


def list_amortization_levels(db: Session) -> list[AmortizationLevelOption]:
    ensure_default_amortization_levels(db)
    return list(
        db.scalars(
            select(AmortizationLevelOption).order_by(
                AmortizationLevelOption.position.asc(),
                AmortizationLevelOption.id.asc(),
            )
        ).all()
    )


def amortization_rubles_map(db: Session) -> dict[str, float]:
    return {row.code: float(row.amount or 0) for row in list_amortization_levels(db)}


def amortization_amount_for(db: Session, code: str | None) -> float:
    raw = (code or "").strip()
    if not raw or raw.upper() in ("NONE", "NO", "0"):
        return 0.0
    ensure_default_amortization_levels(db)
    row = db.scalar(select(AmortizationLevelOption).where(AmortizationLevelOption.code == raw).limit(1))
    if row is None:
        logger.warning("Неизвестный код амортизации: %s", raw)
        return 0.0
    return float(row.amount or 0)


def amortization_choices(
    db: Session,
    current_code: str | None = None,
    *,
    current_amount: float | None = None,
) -> list[AmortizationChoice]:
    """Варианты для формы. Удалённый код визита остаётся в списке как «(удалён)»."""
    rows = list_amortization_levels(db)
    out = [
        AmortizationChoice(
            code=row.code,
            name=row.name,
            amount=float(row.amount or 0),
            description=(row.description or "").strip() or None,
        )
        for row in rows
    ]
    code = _normalize_code(current_code)
    if not code:
        return out
    if any(item.code == code for item in out):
        return out
    amount = float(current_amount) if current_amount is not None else 0.0
    return [
        *out,
        AmortizationChoice(code=code, name="(удалён)", amount=amount, description=None, deleted=True),
    ]


def amortization_label(db: Session, code: str | None) -> str:
    raw = _normalize_code(code)
    if not raw:
        return "—"
    row = db.scalar(select(AmortizationLevelOption).where(AmortizationLevelOption.code == raw).limit(1))
    if row is not None:
        return (row.name or "").strip() or raw
    return raw


def parse_amortization_input(raw: str | None, allowed_codes: Sequence[str]) -> str | None:
    allowed = [str(c).strip() for c in allowed_codes if str(c).strip()]
    s = (raw or "").strip()
    if not s or s.upper() in ("NONE", "NO", "0"):
        return None
    if not allowed:
        raise ValueError("Нет доступных уровней амортизации. Добавьте их в настройках.")
    labels = ", ".join(allowed)
    if s not in allowed:
        raise ValueError(f"Выберите амортизацию: Нет или {labels}.")
    return s


def resolve_amortization_amount(
    db: Session,
    code: str | None,
    *,
    previous_code: str | None = None,
    previous_amount: float | None = None,
) -> float:
    """Сумма для сохранения. Если код уровня не меняли — оставляем снимок визита."""
    if not code:
        return 0.0
    prev = _normalize_code(previous_code)
    if prev and prev == code and previous_amount is not None:
        return float(previous_amount)
    return amortization_amount_for(db, code)


def parse_settings_amount(raw: object | None) -> float:
    s = str(raw or "").strip()
    if not s:
        raise ValueError("Укажите сумму.")
    val = parse_float(s, field_name="amount", min=0.0)
    return float(val)


def parse_settings_name(raw: object | None) -> str:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("Укажите название.")
    if len(text) > MAX_NAME_LEN:
        raise ValueError(f"Название не длиннее {MAX_NAME_LEN} символов.")
    return text


def parse_settings_description(raw: object | None) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if len(text) > MAX_DESCRIPTION_LEN:
        raise ValueError(f"Описание не длиннее {MAX_DESCRIPTION_LEN} символов.")
    return text


def save_amortization_level(
    db: Session,
    *,
    level_id: int | None,
    name: str,
    amount: float,
    description: str | None,
    user_id: int,
) -> AmortizationLevelOption:
    now = utcnow_naive()
    if level_id is None:
        max_pos = db.scalar(select(func.max(AmortizationLevelOption.position))) or 0
        row = AmortizationLevelOption(
            code="TMP",
            name=name,
            amount=float(amount),
            description=description,
            position=int(max_pos) + 1,
            created_at=now,
            updated_at=now,
            updated_by_user_id=user_id,
        )
        db.add(row)
        db.flush()
        row.code = f"L{int(row.id)}"[:MAX_CODE_LEN]
        db.flush()
        write_audit_rows(
            db,
            log_model=AmortizationLevelAuditLog,
            entity_field="amortization_level_id",
            entity_id=row.id,
            changed_by_user_id=user_id,
            changes=diff_fields(
                SimpleNamespace(name=None, amount=None, description=None, code=None),
                row,
                ("code", "name", "amount", "description"),
            ),
        )
        return row

    row = db.get(AmortizationLevelOption, int(level_id))
    if row is None:
        raise ValueError("Уровень не найден.")
    before = SimpleNamespace(name=row.name, amount=row.amount, description=row.description)
    row.name = name
    row.amount = float(amount)
    row.description = description
    row.updated_at = now
    row.updated_by_user_id = user_id
    write_audit_rows(
        db,
        log_model=AmortizationLevelAuditLog,
        entity_field="amortization_level_id",
        entity_id=row.id,
        changed_by_user_id=user_id,
        changes=diff_fields(before, row, ("name", "amount", "description")),
    )
    db.flush()
    return row


def delete_amortization_level(db: Session, *, level_id: int, user_id: int) -> None:
    rows = list_amortization_levels(db)
    if len(rows) <= 1:
        raise ValueError("Нельзя удалить последний уровень амортизации.")
    row = db.get(AmortizationLevelOption, int(level_id))
    if row is None:
        raise ValueError("Уровень не найден.")
    write_audit_rows(
        db,
        log_model=AmortizationLevelAuditLog,
        entity_field="amortization_level_id",
        entity_id=row.id,
        changed_by_user_id=user_id,
        changes=[FieldChange(field_name="deleted", old_value=row.code, new_value=None)],
    )
    db.delete(row)
    db.flush()


def choices_for_template(choices: Sequence[AmortizationChoice]) -> list[dict[str, object]]:
    return [
        {
            "code": c.code,
            "name": c.name,
            "amount": float(c.amount),
            "description": c.description or "",
            "deleted": bool(c.deleted),
        }
        for c in choices
    ]


def _normalize_code(raw: object | None) -> str | None:
    if raw is None:
        return None
    if hasattr(raw, "value"):
        raw = getattr(raw, "value")
    text = str(raw or "").strip()
    if not text or text.upper() in ("NONE", "NO", "0"):
        return None
    return text
