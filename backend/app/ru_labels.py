"""
Человекочитаемые подписи для UI (уровни мастера и т.п.).
"""

from __future__ import annotations

from time import monotonic

from app.db.session import SessionLocal
from app.db.models import MasterLevel, QuestionnaireFieldType, UserRole
from app.setting_keys import (
    MASTER_LEVEL_LABEL_JUNIOR,
    MASTER_LEVEL_LABEL_MIDDLE,
    MASTER_LEVEL_LABEL_SENIOR,
)

# Прайс и уровень мастера в списках — одни и те же формулировки.
RU_MASTER_LEVEL_DEFAULTS: dict[MasterLevel, str] = {
    MasterLevel.JUNIOR: "младший мастер",
    MasterLevel.MIDDLE: "мастер",
    MasterLevel.SENIOR: "старший мастер",
}
RU_MASTER_LEVEL: dict[MasterLevel, str] = dict(RU_MASTER_LEVEL_DEFAULTS)
_MASTER_LEVEL_LABEL_SETTINGS_KEYS: dict[MasterLevel, str] = {
    MasterLevel.JUNIOR: MASTER_LEVEL_LABEL_JUNIOR,
    MasterLevel.MIDDLE: MASTER_LEVEL_LABEL_MIDDLE,
    MasterLevel.SENIOR: MASTER_LEVEL_LABEL_SENIOR,
}
_MASTER_LEVEL_LABELS_CACHE: dict[MasterLevel, str] = dict(RU_MASTER_LEVEL_DEFAULTS)
_MASTER_LEVEL_LABELS_CACHE_EXPIRES_AT: float = 0.0
_MASTER_LEVEL_LABELS_CACHE_TTL_SEC = 10.0


def invalidate_master_level_labels_cache() -> None:
    global _MASTER_LEVEL_LABELS_CACHE_EXPIRES_AT
    _MASTER_LEVEL_LABELS_CACHE_EXPIRES_AT = 0.0


def _read_master_level_labels_from_settings() -> dict[MasterLevel, str]:
    out = dict(RU_MASTER_LEVEL_DEFAULTS)
    db = SessionLocal()
    try:
        from app.db.models import Setting  # local import: avoid import cycles

        for ml, key in _MASTER_LEVEL_LABEL_SETTINGS_KEYS.items():
            row = db.get(Setting, key)
            if row is None:
                continue
            val = str(row.value or "").strip()
            if val:
                out[ml] = val
    except Exception:
        return dict(RU_MASTER_LEVEL_DEFAULTS)
    finally:
        db.close()
    return out


def _get_master_level_labels_cached() -> dict[MasterLevel, str]:
    global _MASTER_LEVEL_LABELS_CACHE, _MASTER_LEVEL_LABELS_CACHE_EXPIRES_AT
    now = monotonic()
    if now < _MASTER_LEVEL_LABELS_CACHE_EXPIRES_AT:
        return _MASTER_LEVEL_LABELS_CACHE
    _MASTER_LEVEL_LABELS_CACHE = _read_master_level_labels_from_settings()
    _MASTER_LEVEL_LABELS_CACHE_EXPIRES_AT = now + _MASTER_LEVEL_LABELS_CACHE_TTL_SEC
    return _MASTER_LEVEL_LABELS_CACHE


def ru_master_level(ml: MasterLevel | str | None) -> str:
    """Подпись уровня мастера для шаблонов и выпадающих списков."""
    if ml is None:
        return "уровень не указан"
    if isinstance(ml, str):
        try:
            ml = MasterLevel(ml)
        except ValueError:
            return ml
    return _get_master_level_labels_cached().get(ml, str(ml))


RU_USER_ROLE: dict[UserRole, str] = {
    UserRole.ADMIN_SUPER: "Суперадмин",
    UserRole.ADMIN: "Админ",
    UserRole.MASTER: "Мастер",
    UserRole.TECHSPEC: "Техспец",
}

RU_USER_ROLE_PAYOUT: dict[UserRole, str] = {
    UserRole.ADMIN_SUPER: "суперадмин",
    UserRole.ADMIN: "админ",
    UserRole.MASTER: "мастер",
    UserRole.TECHSPEC: "техспец",
}


def ru_user_roles_payout_suffix(roles: list[UserRole]) -> str:
    """Подпись ролей в скобках для формы выплат (нижний регистр, порядок: суперадмин → админ → мастер)."""
    order = {UserRole.TECHSPEC: -1, UserRole.ADMIN_SUPER: 0, UserRole.ADMIN: 1, UserRole.MASTER: 2}
    uniq = sorted(set(roles), key=lambda r: order[r])
    return ", ".join(RU_USER_ROLE_PAYOUT[r] for r in uniq)


def ru_user_role(r: UserRole | str) -> str:
    if isinstance(r, str):
        try:
            r = UserRole(r)
        except ValueError:
            return r
    return RU_USER_ROLE.get(r, str(r.value))


RU_QUESTIONNAIRE_FIELD_TYPE: dict[QuestionnaireFieldType, str] = {
    QuestionnaireFieldType.TEXT: "однострочный текст",
    QuestionnaireFieldType.NUMBER: "число",
    QuestionnaireFieldType.TEXTAREA: "многострочный текст",
    QuestionnaireFieldType.CHECKBOX: "галочка",
    QuestionnaireFieldType.SELECT: "выбор из списка",
}


def ru_questionnaire_field_type(ft: QuestionnaireFieldType | str) -> str:
    if isinstance(ft, str):
        try:
            ft = QuestionnaireFieldType(ft)
        except ValueError:
            return ft
    return RU_QUESTIONNAIRE_FIELD_TYPE.get(ft, str(ft))


def format_price_integer_rub(amount: float | int | None) -> str | None:
    """Цена для просмотра каталога: целое число и символ ₽ (без копеек и без .0)."""
    if amount is None:
        return None
    n = int(round(float(amount)))
    return f"{n} ₽"
