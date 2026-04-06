"""
Человекочитаемые подписи для UI (уровни мастера и т.п.).
"""

from __future__ import annotations

from app.db.models import MasterLevel, QuestionnaireFieldType

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
