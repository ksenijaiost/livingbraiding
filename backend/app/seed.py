from __future__ import annotations

"""
Dev seeding.

This runs on app startup to keep local development frictionless:
- default admin and master accounts
- default settings
- placeholder material prices

In production you may want to disable this or make it explicit via a CLI task.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Kit,
    MaterialPriceCurrent,
    MaterialType,
    QuestionnaireFieldType,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    Setting,
    SubcategoryQuestionnaireField,
    User,
    UserRole,
    MasterLevel,
)
from app.security import hash_password
from app.seed_catalog_vsy_golova import ensure_vsy_golova_catalog
from app.seed_catalog_zakaz import ensure_zakaz_catalog
from app.seed_catalog_malishki_muzhchiny import ensure_malishki_muzhchiny_catalog
from app.seed_catalog_miniatyura import ensure_miniatyura_catalog
from app.seed_catalog_narashivanie import ensure_narashivanie_catalog
from app.seed_catalog_prodazha_materiala import ensure_prodazha_materiala_catalog
from app.seed_catalog_snjatie_ukhod import ensure_snjatie_ukhod_catalogs


def ensure_seed_data(db: Session) -> None:
    # Settings
    salon = db.get(Setting, "salon_cut_pct")
    if not salon:
        db.add(Setting(key="salon_cut_pct", value="0.3"))

    edit_days = db.get(Setting, "edit_window_days")
    if not edit_days:
        db.add(Setting(key="edit_window_days", value="2"))

    tz = db.get(Setting, "display_timezone")
    if not tz:
        db.add(Setting(key="display_timezone", value="Asia/Novosibirsk"))

    # Default material prices: ₽ за 100 г → ₽/г (админ может поменять в настройках)
    defaults = {
        MaterialType.KANEKALON: 4.0,  # 400 ₽ / 100 г
        MaterialType.KUDRI: 8.0,  # 800 ₽ / 100 г
    }
    for mt, default_per_gram in defaults.items():
        row = db.get(MaterialPriceCurrent, mt)
        if not row:
            db.add(MaterialPriceCurrent(material_type=mt, price_per_gram=default_per_gram))

    # Users
    if not db.scalar(select(User).where(User.username == "admin")):
        db.add(
            User(
                username="admin",
                display_name="Админ",
                role=UserRole.ADMIN_SUPER,
                password_hash=hash_password("admin"),
                is_active=True,
                master_level=None,
            )
        )

    if not db.scalar(select(User).where(User.username == "master1")):
        db.add(
            User(
                username="master1",
                display_name="Мастер 1",
                role=UserRole.MASTER,
                password_hash=hash_password("master1"),
                is_active=True,
                master_level=MasterLevel.JUNIOR,
            )
        )

    if not db.scalar(select(User).where(User.username == "master2")):
        db.add(
            User(
                username="master2",
                display_name="Мастер 2",
                role=UserRole.MASTER,
                password_hash=hash_password("master2"),
                is_active=True,
                master_level=MasterLevel.MIDDLE,
            )
        )

    _ensure_vsy_golova_catalog_and_kits(db)

    db.commit()


def _ensure_inlay_subcategory_questionnaire_fields(db: Session, subcategory_id: int) -> None:
    """Общие вопросы вплетения для «В 2 руки» и «в 4 руки» (подкатегория в БД)."""
    if db.scalar(
        select(SubcategoryQuestionnaireField.id).where(
            SubcategoryQuestionnaireField.subcategory_id == subcategory_id,
            SubcategoryQuestionnaireField.field_key == "inlay_bases_count",
        )
    ):
        return
    db.add(
        SubcategoryQuestionnaireField(
            subcategory_id=subcategory_id,
            field_key="inlay_bases_count",
            field_type=QuestionnaireFieldType.NUMBER,
            label="Количество баз",
            required=True,
            sort_order=10,
            min_value=0.0,
        )
    )
    db.add(
        SubcategoryQuestionnaireField(
            subcategory_id=subcategory_id,
            field_key="inlay_blanks_count",
            field_type=QuestionnaireFieldType.NUMBER,
            label="Количество заготовок (в работе)",
            required=True,
            sort_order=20,
            min_value=0.0,
        )
    )
    db.add(
        SubcategoryQuestionnaireField(
            subcategory_id=subcategory_id,
            field_key="inlay_service_comment",
            field_type=QuestionnaireFieldType.TEXTAREA,
            label="Комментарий по услуге",
            required=False,
            sort_order=30,
            placeholder="Разметка, несколько заготовок в базу…",
        )
    )


def _deactivate_legacy_inlay_4h_service(db: Session) -> None:
    """
    Раньше услуга называлась «в 4 руки»; актуальная — «В 4 руки» из JSON.
    Старую строку оставляем неактивной как пример в каталоге (при каждом старте сида снова is_active=False).
    """
    svc = db.scalar(
        select(Service)
        .join(ServiceSubcategory, Service.subcategory_id == ServiceSubcategory.id)
        .join(ServiceCategory, ServiceSubcategory.category_id == ServiceCategory.id)
        .where(
            ServiceCategory.name == "Вся голова",
            ServiceSubcategory.name == "Вплетение комплекта",
            Service.name == "в 4 руки",
        )
    )
    if svc is not None:
        svc.is_active = False


def _ensure_vsy_golova_catalog_and_kits(db: Session) -> None:
    """Каталоги из JSON + анкета вплетения + демо-комплекты."""
    ensure_vsy_golova_catalog(db)
    ensure_zakaz_catalog(db)
    ensure_malishki_muzhchiny_catalog(db)
    ensure_miniatyura_catalog(db)
    ensure_narashivanie_catalog(db)
    ensure_prodazha_materiala_catalog(db)
    ensure_snjatie_ukhod_catalogs(db)
    _deactivate_legacy_inlay_4h_service(db)

    sub = db.scalar(
        select(ServiceSubcategory)
        .join(ServiceCategory, ServiceSubcategory.category_id == ServiceCategory.id)
        .where(
            ServiceCategory.name == "Вся голова",
            ServiceSubcategory.name == "Вплетение комплекта",
        )
    )
    if sub:
        _ensure_inlay_subcategory_questionnaire_fields(db, sub.id)

    if not db.scalar(select(Kit).where(Kit.sku == "DEMO-001")):
        db.add(
            Kit(
                sku="DEMO-001",
                title="Комплект, 70 заготовок (пример для склада)",
                description="Для проверки «из наличия»",
                pieces_total=70,
                pieces_available=70,
                blank_type_de=True,
                blank_type_se=False,
                stock_price_total=3500.0,
                cost_total=None,
                is_in_stock=True,
                is_archived=False,
            )
        )
    if not db.scalar(select(Kit).where(Kit.sku == "DEMO-002")):
        db.add(
            Kit(
                sku="DEMO-002",
                title="Комплект, 10 заготовок (пример)",
                description="Для доп. заготовок (свой + из наличия)",
                pieces_total=10,
                pieces_available=10,
                blank_type_de=True,
                blank_type_se=True,
                stock_price_total=800.0,
                cost_total=None,
                is_in_stock=True,
                is_archived=False,
            )
        )

