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
    CategoryQuestionnaireField,
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
from app.seed_studio_expenses_catalog import ensure_studio_expense_catalog


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

    ensure_studio_expense_catalog(db)

    db.commit()


def _ensure_category_questionnaire_field(
    db: Session,
    *,
    category_id: int,
    field_key: str,
    field_type: QuestionnaireFieldType,
    label: str,
    required: bool,
    sort_order: int,
    placeholder: str | None = None,
    min_value: float | None = None,
) -> None:
    if db.scalar(
        select(CategoryQuestionnaireField.id).where(
            CategoryQuestionnaireField.category_id == category_id,
            CategoryQuestionnaireField.field_key == field_key,
        )
    ):
        return
    db.add(
        CategoryQuestionnaireField(
            category_id=category_id,
            field_key=field_key,
            field_type=field_type,
            label=label,
            required=required,
            sort_order=sort_order,
            placeholder=placeholder,
            min_value=min_value,
        )
    )


def _ensure_category_questionnaire_vsy_golova_and_miniatyura(db: Session) -> None:
    for cat_name in ("Вся голова", "Миниатюра"):
        cat = db.scalar(select(ServiceCategory).where(ServiceCategory.name == cat_name))
        if not cat:
            continue
        _ensure_category_questionnaire_field(
            db,
            category_id=cat.id,
            field_key="inlay_bases_count",
            field_type=QuestionnaireFieldType.NUMBER,
            label="Количество баз",
            required=True,
            sort_order=10,
            min_value=0.0,
        )
        _ensure_category_questionnaire_field(
            db,
            category_id=cat.id,
            field_key="inlay_blanks_count",
            field_type=QuestionnaireFieldType.NUMBER,
            label="Количество заготовок (в работе)",
            required=True,
            sort_order=20,
            min_value=0.0,
        )
        _ensure_category_questionnaire_field(
            db,
            category_id=cat.id,
            field_key="inlay_service_comment",
            field_type=QuestionnaireFieldType.TEXTAREA,
            label="Уточнение / комментарий по услуге",
            required=False,
            sort_order=30,
            placeholder="Разметка, несколько заготовок в базу…",
        )
        _ensure_category_questionnaire_field(
            db,
            category_id=cat.id,
            field_key="material_description",
            field_type=QuestionnaireFieldType.TEXTAREA,
            label="Описание про материал",
            required=False,
            sort_order=40,
            placeholder="Необязательно",
        )


def _remove_inlay_fields_from_subcategory_vpletenie(db: Session) -> None:
    sub = db.scalar(
        select(ServiceSubcategory)
        .join(ServiceCategory, ServiceSubcategory.category_id == ServiceCategory.id)
        .where(
            ServiceCategory.name == "Вся голова",
            ServiceSubcategory.name == "Вплетение комплекта",
        )
    )
    if not sub:
        return
    for fk in ("inlay_bases_count", "inlay_blanks_count", "inlay_service_comment"):
        row = db.scalar(
            select(SubcategoryQuestionnaireField).where(
                SubcategoryQuestionnaireField.subcategory_id == sub.id,
                SubcategoryQuestionnaireField.field_key == fk,
            )
        )
        if row:
            db.delete(row)


def _ensure_sphinx_subcategory_questionnaire_fields(db: Session) -> None:
    for sub_name in ("Сфинкс дети", "Сфинкс взрослый"):
        sub = db.scalar(
            select(ServiceSubcategory)
            .join(ServiceCategory, ServiceSubcategory.category_id == ServiceCategory.id)
            .where(
                ServiceCategory.name == "Вся голова",
                ServiceSubcategory.name == sub_name,
            )
        )
        if not sub:
            continue
        if not db.scalar(
            select(SubcategoryQuestionnaireField.id).where(
                SubcategoryQuestionnaireField.subcategory_id == sub.id,
                SubcategoryQuestionnaireField.field_key == "sphinx_braids_count",
            )
        ):
            db.add(
                SubcategoryQuestionnaireField(
                    subcategory_id=sub.id,
                    field_key="sphinx_braids_count",
                    field_type=QuestionnaireFieldType.NUMBER,
                    label="Количество брейдов",
                    required=False,
                    sort_order=5,
                    min_value=0.0,
                )
            )
        if not db.scalar(
            select(SubcategoryQuestionnaireField.id).where(
                SubcategoryQuestionnaireField.subcategory_id == sub.id,
                SubcategoryQuestionnaireField.field_key == "sphinx_nape_bases_count",
            )
        ):
            db.add(
                SubcategoryQuestionnaireField(
                    subcategory_id=sub.id,
                    field_key="sphinx_nape_bases_count",
                    field_type=QuestionnaireFieldType.NUMBER,
                    label="Количество баз на затылке",
                    required=False,
                    sort_order=6,
                    min_value=0.0,
                )
            )


def _ensure_thermo_subcategory_flag(db: Session) -> None:
    sub = db.scalar(
        select(ServiceSubcategory)
        .join(ServiceCategory, ServiceSubcategory.category_id == ServiceCategory.id)
        .where(
            ServiceCategory.name == "Вся голова",
            ServiceSubcategory.name == "Термозамещение",
        )
    )
    if sub:
        sub.show_thermo_visit = True


def _ensure_subcategory_kit_and_material_flags(db: Session) -> None:
    kit_pairs = (
        ("Вся голова", "Вплетение комплекта"),
        ("Миниатюра", "Затылок комплект"),
        ("Мужчины", "Вплетение комплекта"),
    )
    for cname, sname in kit_pairs:
        sub = db.scalar(
            select(ServiceSubcategory)
            .join(ServiceCategory, ServiceSubcategory.category_id == ServiceCategory.id)
            .where(ServiceCategory.name == cname, ServiceSubcategory.name == sname)
        )
        if sub:
            sub.show_kit_section = True

    bez = db.scalar(
        select(ServiceSubcategory)
        .join(ServiceCategory, ServiceSubcategory.category_id == ServiceCategory.id)
        .where(ServiceCategory.name == "Миниатюра", ServiceSubcategory.name == "Без материала")
    )
    if bez:
        bez.show_material_description = False


def _ensure_service_kit_material_overrides(db: Session) -> None:
    rows = db.scalars(
        select(Service)
        .join(ServiceSubcategory, Service.subcategory_id == ServiceSubcategory.id)
        .join(ServiceCategory, ServiceSubcategory.category_id == ServiceCategory.id)
        .where(
            ServiceCategory.name == "Вся голова",
            ServiceSubcategory.name == "Сфинкс взрослый",
            Service.name.startswith("Сфинкс заготовки"),
        )
    ).all()
    for s in rows:
        s.kit_section_override = True

    for cname, sname, svc_name in (
        ("Миниатюра", "Висок", "2 брейда без материала"),
        ("Миниатюра", "Висок", "3 Брейда без материала"),
        ("Миниатюра", "Ободок", "Без канекалона 1 брейд"),
    ):
        svc = db.scalar(
            select(Service)
            .join(ServiceSubcategory, Service.subcategory_id == ServiceSubcategory.id)
            .join(ServiceCategory, ServiceSubcategory.category_id == ServiceCategory.id)
            .where(
                ServiceCategory.name == cname,
                ServiceSubcategory.name == sname,
                Service.name == svc_name,
            )
        )
        if svc:
            svc.hide_material_description = True


def _move_muzhchiny_services_between_subcategories(db: Session) -> None:
    def _move(cat_name: str, from_sub: str, to_sub: str, service_name: str) -> None:
        c = db.scalar(select(ServiceCategory).where(ServiceCategory.name == cat_name))
        if not c:
            return
        o = db.scalar(
            select(ServiceSubcategory).where(
                ServiceSubcategory.category_id == c.id,
                ServiceSubcategory.name == from_sub,
            )
        )
        n = db.scalar(
            select(ServiceSubcategory).where(
                ServiceSubcategory.category_id == c.id,
                ServiceSubcategory.name == to_sub,
            )
        )
        if not o or not n:
            return
        svc = db.scalar(
            select(Service).where(Service.subcategory_id == o.id, Service.name == service_name)
        )
        if svc:
            svc.subcategory_id = n.id

    _move(
        "Мужчины",
        "Классика (точка)",
        "Вплетение комплекта",
        "Де-Дреды изготовление на андеркат до плеч 100",
    )
    _move(
        "Мужчины",
        "Классика (точка)",
        "Вплетение комплекта",
        "Полу8 поштучно (без учета комплекта)",
    )
    _move("Мужчины", "Классика (точка)", "Уход", "Стрижка под андеркат")


def _ensure_visit_questionnaire_layout(db: Session) -> None:
    _ensure_category_questionnaire_vsy_golova_and_miniatyura(db)
    _remove_inlay_fields_from_subcategory_vpletenie(db)
    _ensure_sphinx_subcategory_questionnaire_fields(db)
    _ensure_thermo_subcategory_flag(db)
    _ensure_subcategory_kit_and_material_flags(db)
    _ensure_service_kit_material_overrides(db)
    _move_muzhchiny_services_between_subcategories(db)


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

    _ensure_visit_questionnaire_layout(db)

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

