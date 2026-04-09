"""Справочник категорий и подкатегорий расходов студии (этап 8)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import StudioExpenseCategory, StudioExpenseSubcategory


# (категория, sort_order, [подкатегории по порядку])
STUDIO_EXPENSE_TREE: list[tuple[str, int, list[str]]] = [
    (
        "Закуп материала",
        10,
        ["Кудри", "Канек"],
    ),
    (
        "Расходники рабочие",
        20,
        [
            "Резинки, заколки, расчёски, украшения",
            "Уход: шампуни, маски, бальзамы, спреи",
        ],
    ),
    (
        "Расходники хоз",
        30,
        [
            "Туалетная бумага, салфетки, чистящие и моющие, антисептик, тряпки, губки",
        ],
    ),
    (
        "Расходы кухня",
        40,
        [
            "Съедобные",
            "Несъедобные (кружки, посуда, подносы)",
        ],
    ),
    (
        "Бытовые",
        50,
        [
            "Ипотека",
            "Амортизация помещения",
            "Амортизация оборудования",
            "Коммуналка",
            "Интернет",
            "Подписки/сервисы",
        ],
    ),
    (
        "Другие расходы",
        60,
        [
            "Подарки и праздники",
            "Корпоративные расходы",
            "Типография для студии",
            "Печатная продукция для обучений",
            "Аптечка",
            "Спонсорство",
            "Повышение сотрудников (обучения)",
            "Непредвиденные расходы",
            "Выплаты сторонним мастерам",
        ],
    ),
]


def ensure_studio_expense_catalog(db: Session) -> None:
    if db.scalar(select(StudioExpenseCategory).limit(1)):
        return
    for cat_name, cat_order, sub_names in STUDIO_EXPENSE_TREE:
        cat = StudioExpenseCategory(name=cat_name, is_active=True, sort_order=cat_order)
        db.add(cat)
        db.flush()
        for i, sub_name in enumerate(sub_names):
            db.add(
                StudioExpenseSubcategory(
                    category_id=cat.id,
                    name=sub_name,
                    is_active=True,
                    sort_order=(i + 1) * 10,
                )
            )
