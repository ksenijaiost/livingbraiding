"""
Человекочитаемые подписи для UI (уровни мастера и т.п.).
"""

from __future__ import annotations

from app.db.models import MasterLevel

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
