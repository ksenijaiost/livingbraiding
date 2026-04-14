"""
Человекочитаемые подписи для UI (уровни мастера и т.п.).
"""

from __future__ import annotations

from app.db.models import MasterLevel, QuestionnaireFieldType, UserRole

# Прайс и уровень мастера в списках — одни и те же формулировки.
RU_MASTER_LEVEL: dict[MasterLevel, str] = {
    MasterLevel.JUNIOR: "младший мастер",
    MasterLevel.MIDDLE: "мастер",
    MasterLevel.SENIOR: "старший мастер",
}


def ru_master_level(ml: MasterLevel | str | None) -> str:
    """Подпись уровня мастера для шаблонов и выпадающих списков."""
    if ml is None:
        return "уровень не указан"
    if isinstance(ml, str):
        try:
            ml = MasterLevel(ml)
        except ValueError:
            return ml
    return RU_MASTER_LEVEL.get(ml, str(ml))


RU_USER_ROLE: dict[UserRole, str] = {
    UserRole.ADMIN_SUPER: "Суперадмин",
    UserRole.ADMIN: "Админ",
    UserRole.MASTER: "Мастер",
}

RU_USER_ROLE_PAYOUT: dict[UserRole, str] = {
    UserRole.ADMIN_SUPER: "суперадмин",
    UserRole.ADMIN: "админ",
    UserRole.MASTER: "мастер",
}


def ru_user_roles_payout_suffix(roles: list[UserRole]) -> str:
    """Подпись ролей в скобках для формы выплат (нижний регистр, порядок: суперадмин → админ → мастер)."""
    order = {UserRole.ADMIN_SUPER: 0, UserRole.ADMIN: 1, UserRole.MASTER: 2}
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
