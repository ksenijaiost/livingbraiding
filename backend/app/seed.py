from __future__ import annotations

"""
Dev seeding.

This runs on app startup to keep local development frictionless:
- default admin and master accounts
- default settings
- placeholder material prices

In production you may want to disable this or make it explicit via a CLI task.
"""

import json
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    Booking,
    BookingKind,
    BookingStaff,
    BookingStaffKind,
    BookingStatus,
    CatalogProduct,
    CategoryQuestionnaireField,
    Kit,
    KitBlanksCondition,
    MaterialPriceCurrent,
    MaterialType,
    QuestionnaireFieldType,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    ServiceQuestionnaireField,
    Setting,
    SubcategoryQuestionnaireField,
    User,
    UserRole,
    UserRoleAssignment,
    MasterLevel,
    Client,
    PayrollPeriod,
    ProductSale,
    ProductSaleKind,
    StudioExpense,
    StudioExpenseSubcategory,
    Visit,
    VisitClientType,
    VisitMaster,
    VisitPriceType,
    VisitService,
    WorkForInventory,
    WorkForInventoryStaff,
    WorkKind,
    WorkScope,
)
from app.security import hash_password
from app.user_roles import set_user_roles
from app.seed_catalog_vsy_golova import ensure_vsy_golova_catalog
from app.seed_catalog_zakaz import ensure_zakaz_catalog
from app.seed_catalog_malishki_muzhchiny import ensure_malishki_muzhchiny_catalog
from app.seed_catalog_miniatyura import ensure_miniatyura_catalog
from app.seed_catalog_narashivanie import ensure_narashivanie_catalog
from app.seed_catalog_prodazha_materiala import ensure_prodazha_materiala_catalog
from app.seed_catalog_snjatie_ukhod import ensure_snjatie_ukhod_catalogs
from app.payroll_fund import sync_operational_payroll_postings
from app.product_sale_material import finalize_material_sale_fields
from app.seed_studio_expenses_catalog import ensure_studio_expense_catalog
from app.setting_keys import (
    AUDIT_RETENTION_MONTHS,
    DISPLAY_TIMEZONE,
    EDIT_WINDOW_DAYS,
    KIT_MAX_RESERVES_PER_KIT,
    SALON_CUT_PCT,
)
from app.zakaz_blanks import zakaz_blank_defs
from app.time_utils import utcnow_naive


def _ensure_demo_user_role_assignments(db: Session) -> None:
    """Идемпотентно: заполняет user_role_assignments для демо-логинов, если таблица ещё пуста для пользователя."""
    for username, roles in (
        ("admin", [UserRole.ADMIN_SUPER, UserRole.MASTER]),
        ("master1", [UserRole.MASTER]),
        ("master2", [UserRole.MASTER]),
    ):
        u = db.scalar(select(User).where(User.username == username))
        if not u:
            continue
        has_any = db.scalar(
            select(UserRoleAssignment.id).where(UserRoleAssignment.user_id == u.id).limit(1)
        )
        if has_any:
            continue
        set_user_roles(db, u, roles)


def ensure_seed_data(db: Session) -> None:
    """
    Legacy entrypoint (kept for compatibility).

    Prefer:
    - ensure_prod_seed_data: safe defaults + catalogs/prices (no demo operational data)
    - ensure_dev_seed_data: production seed + demo users + demo operational data
    """
    ensure_dev_seed_data(db)


def ensure_prod_seed_data(db: Session) -> None:
    """Idempotent seed safe for production: settings, price catalogs, expense catalog.

    Does NOT create demo users/clients/visits/sales/etc.
    """
    # Settings
    salon = db.get(Setting, SALON_CUT_PCT)
    if not salon:
        db.add(Setting(key=SALON_CUT_PCT, value="0.5"))
    elif (salon.updated_at is None) and (salon.updated_by_user_id is None) and (str(salon.value).strip() == "0.3"):
        # One-time migration from old default (0.3) to the current default (0.5),
        # but only if the setting was never edited by a user.
        salon.value = "0.5"

    edit_days = db.get(Setting, EDIT_WINDOW_DAYS)
    if not edit_days:
        db.add(Setting(key=EDIT_WINDOW_DAYS, value="2"))

    audit_retention = db.get(Setting, AUDIT_RETENTION_MONTHS)
    if not audit_retention:
        db.add(Setting(key=AUDIT_RETENTION_MONTHS, value="6"))

    tz = db.get(Setting, DISPLAY_TIMEZONE)
    if not tz:
        db.add(Setting(key=DISPLAY_TIMEZONE, value="Asia/Novosibirsk"))

    kit_mx = db.get(Setting, KIT_MAX_RESERVES_PER_KIT)
    if not kit_mx:
        db.add(Setting(key=KIT_MAX_RESERVES_PER_KIT, value="3"))

    # Default material prices: ₽ за 100 г → ₽/г (админ может поменять в настройках)
    defaults = {
        MaterialType.KANEKALON: 4.0,  # 400 ₽ / 100 г
        MaterialType.KUDRI: 8.0,  # 800 ₽ / 100 г
    }
    for mt, default_per_gram in defaults.items():
        row = db.get(MaterialPriceCurrent, mt)
        if not row:
            db.add(MaterialPriceCurrent(material_type=mt, price_per_gram=default_per_gram))

    # Service catalogs + catalog_products price lists (idempotent)
    _ensure_vsy_golova_catalog_and_kits(db, include_demo_kits=False)

    # Expense catalog (idempotent)
    ensure_studio_expense_catalog(db)

    db.commit()


def ensure_dev_seed_data(db: Session) -> None:
    """Idempotent development seed: prod seed + demo accounts + demo operational data."""
    ensure_prod_seed_data(db)

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

    db.flush()
    _ensure_demo_user_role_assignments(db)

    # Service catalogs + kits + derived price lists in catalog_products
    _ensure_vsy_golova_catalog_and_kits(db, include_demo_kits=True)

    _ensure_demo_operational_data(db)

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
        # «Количество заготовок» на категории только у «Миниатюра»; у «Вся голова» — подкатегория «Вплетение комплекта»
        # и отдельные услуги «Сфинкс взрослый» с блоком комплекта.
        if cat_name == "Миниатюра":
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


def _drop_vsya_golova_category_inlay_blanks(db: Session) -> None:
    """Убираем устаревшее поле категории (перенесено на подкатегорию / услуги)."""
    cat = db.scalar(select(ServiceCategory).where(ServiceCategory.name == "Вся голова"))
    if not cat:
        return
    row = db.scalar(
        select(CategoryQuestionnaireField).where(
            CategoryQuestionnaireField.category_id == cat.id,
            CategoryQuestionnaireField.field_key == "inlay_blanks_count",
        )
    )
    if row:
        db.delete(row)


def _ensure_inlay_blanks_vpletenie_subcat_and_sphinx_kit_services(db: Session) -> None:
    sub = db.scalar(
        select(ServiceSubcategory)
        .join(ServiceCategory, ServiceSubcategory.category_id == ServiceCategory.id)
        .where(
            ServiceCategory.name == "Вся голова",
            ServiceSubcategory.name == "Вплетение комплекта",
        )
    )
    if sub and not db.scalar(
        select(SubcategoryQuestionnaireField.id).where(
            SubcategoryQuestionnaireField.subcategory_id == sub.id,
            SubcategoryQuestionnaireField.field_key == "inlay_blanks_count",
        )
    ):
        db.add(
            SubcategoryQuestionnaireField(
                subcategory_id=sub.id,
                field_key="inlay_blanks_count",
                field_type=QuestionnaireFieldType.NUMBER,
                label="Количество заготовок (в работе)",
                required=True,
                sort_order=20,
                min_value=0.0,
            )
        )

    for svc in db.scalars(
        select(Service)
        .join(ServiceSubcategory, Service.subcategory_id == ServiceSubcategory.id)
        .join(ServiceCategory, ServiceSubcategory.category_id == ServiceCategory.id)
        .where(
            ServiceCategory.name == "Вся голова",
            ServiceSubcategory.name == "Сфинкс взрослый",
            or_(
                Service.kit_section_override.is_(True),
                Service.name.startswith("Сфинкс заготовки"),
            ),
        )
    ).all():
        if db.scalar(
            select(ServiceQuestionnaireField.id).where(
                ServiceQuestionnaireField.service_id == svc.id,
                ServiceQuestionnaireField.field_key == "inlay_blanks_count",
            )
        ):
            continue
        db.add(
            ServiceQuestionnaireField(
                service_id=svc.id,
                field_key="inlay_blanks_count",
                field_type=QuestionnaireFieldType.NUMBER,
                label="Количество заготовок (в работе)",
                required=True,
                sort_order=20,
                min_value=0.0,
            )
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
    # Дубликаты с категорией убираем только для баз и комментария; «заготовки» живут на подкатегории.
    for fk in ("inlay_bases_count", "inlay_service_comment"):
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
            svc.material_description_override = False


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
    _drop_vsya_golova_category_inlay_blanks(db)
    _remove_inlay_fields_from_subcategory_vpletenie(db)
    _ensure_sphinx_subcategory_questionnaire_fields(db)
    _ensure_thermo_subcategory_flag(db)
    _ensure_subcategory_kit_and_material_flags(db)
    _ensure_service_kit_material_overrides(db)
    db.flush()
    # После выставления kit_section_override у «Сфинкс заготовки…» (видно следующему SELECT).
    _ensure_inlay_blanks_vpletenie_subcat_and_sphinx_kit_services(db)
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


def _upsert_catalog_product(
    db: Session,
    *,
    category_name: str,
    subcategory_name: str,
    name: str,
    price: float | None,
    meta: dict | None,
    sort_order: int,
) -> None:
    row = db.scalar(
        select(CatalogProduct).where(
            CatalogProduct.category_name == category_name,
            CatalogProduct.subcategory_name == subcategory_name,
            CatalogProduct.name == name,
        )
    )
    meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
    if row is None:
        db.add(
            CatalogProduct(
                category_name=category_name,
                subcategory_name=subcategory_name,
                name=name,
                price=price,
                meta_json=meta_json,
                sort_order=sort_order,
                is_active=True,
            )
        )
        return
    row.is_active = True
    row.price = price
    row.meta_json = meta_json
    row.sort_order = sort_order


def _deactivate_obsolete_zakaz_blank_catalog_rows(db: Session, *, obsolete_kit_keys: frozenset[str]) -> None:
    """Снять с прайса строки с устаревшими kit_key (переименования / разбиение позиций)."""
    rows = list(
        db.scalars(
            select(CatalogProduct).where(
                CatalogProduct.category_name == "Заказ",
                CatalogProduct.subcategory_name == "Заготовки поштучно",
            )
        ).all()
    )
    for r in rows:
        try:
            meta = json.loads(r.meta_json or "{}")
        except Exception:
            continue
        if not isinstance(meta, dict):
            continue
        k = str(meta.get("kit_key") or "").strip()
        if k in obsolete_kit_keys:
            r.is_active = False


def _ensure_zakaz_products_catalog(db: Session) -> None:
    """
    Прайс «Заказ» в catalog_products.

    Источник финансовых полей (master_pay/studio_pay/fixed_expense) берём из service-catalog «Заказ»,
    чтобы не дублировать логику и не потерять старые коэффициенты. Клиентскую цену (price) задаём здесь.
    """
    cat = db.scalar(select(ServiceCategory).where(ServiceCategory.name == "Заказ"))

    # Финансовые поля теперь живут в catalog_products.meta_json; из service-catalog их не тянем.
    fin_map: dict[tuple[str, str], dict] = {}

    # ---- Blanks (used for kit price + master pay) ----
    so = 0
    for row in zakaz_blank_defs():
        meta = {
            "kit_key": row.key,
            "ignore_in_calc": bool(row.ignore_in_client_calc or row.key is None),
            "master_pay": float(row.work_pay),
            "is_used_in_kit_form": bool(row.include_in_kit_form),
            "is_bu": bool(row.is_bu),
        }
        _upsert_catalog_product(
            db,
            category_name="Заказ",
            subcategory_name="Заготовки поштучно",
            name=row.display_name,
            price=float(row.price),
            meta=meta,
            sort_order=so,
        )
        so += 1

    _deactivate_obsolete_zakaz_blank_catalog_rows(
        db,
        obsolete_kit_keys=frozenset({"SE_BRAID_FREE_TIP", "DE_BRAID_NEW_FMT", "DE_DREAD_FREE_TIP"}),
    )

    # ---- Correction ----
    corr_prices_overrides = {
        "Стрижка (1шт)": 5.0,
        "Одевание на круг": 100.0,
        "Стирка (с коррекцией)": 400.0,
        "Стирка (без коррекции)": 1200.0,
        "Отпаривание": 200.0,
        "Почасовая коррекция заготовок (1 ч)": 600.0,
    }
    if cat:
        corr_sub = db.scalar(
            select(ServiceSubcategory).where(
                ServiceSubcategory.category_id == cat.id, ServiceSubcategory.name == "Коррекция комплекта"
            )
        )
    else:
        corr_sub = None
    corr_rows = (
        list(db.scalars(select(Service).where(Service.subcategory_id == corr_sub.id)).all())
        if corr_sub is not None
        else []
    )
    # Сохраняем/переносим все строки коррекции, чтобы выплаты не обнулились.
    # Для известных позиций применяем новые цены.
    so = 0
    for s in corr_rows:
        svc_name = s.name
        meta = fin_map.get(("Коррекция комплекта", svc_name), None) or {}
        price = (
            float(corr_prices_overrides[svc_name])
            if svc_name in corr_prices_overrides
            else (float(s.price_middle_from) if s.price_middle_from is not None else None)
        )
        _upsert_catalog_product(
            db,
            category_name="Заказ",
            subcategory_name="Коррекция комплекта",
            name=svc_name,
            price=price,
            meta=meta,
            sort_order=so,
        )
        so += 1

    # ---- Rubber / tails ----
    # Copy existing service-catalog rows as-is (price may be adjusted later in UI).
    if cat:
        rows = list(
            db.scalars(
                select(Service)
                .join(ServiceSubcategory, Service.subcategory_id == ServiceSubcategory.id)
                .join(ServiceCategory, ServiceSubcategory.category_id == ServiceCategory.id)
                .where(ServiceSubcategory.name == "Хвосты/резинки", ServiceCategory.name == "Заказ")
            ).all()
        )
        so = 0
        for s in rows:
            meta = fin_map.get(("Хвосты/резинки", s.name), None) or {}
            price = float(s.price_middle_from) if s.price_middle_from is not None else None
            _upsert_catalog_product(
                db,
                category_name="Заказ",
                subcategory_name="Хвосты/резинки",
                name=s.name,
                price=price,
                meta=meta,
                sort_order=so,
            )
            so += 1


def _ensure_prodazha_materiala_products_catalog(db: Session) -> None:
    cat = db.scalar(select(ServiceCategory).where(ServiceCategory.name == "Продажа материала"))
    if not cat:
        return
    subs = list(
        db.scalars(
            select(ServiceSubcategory)
            .where(ServiceSubcategory.category_id == cat.id, ServiceSubcategory.is_active.is_(True))
            .order_by(ServiceSubcategory.id.asc())
        ).all()
    )
    so = 0
    for sub in subs:
        rows = list(
            db.scalars(
                select(Service)
                .where(Service.subcategory_id == sub.id, Service.is_active.is_(True))
                .order_by(Service.id.asc())
            ).all()
        )
        for s in rows:
            _upsert_catalog_product(
                db,
                category_name="Продажа материала",
                subcategory_name=sub.name,
                name=s.name,
                price=float(s.price_middle_from) if s.price_middle_from is not None else None,
                meta={"master_pay": None, "fixed_expense": None},
                sort_order=so,
            )
            so += 1

def _ensure_vsy_golova_catalog_and_kits(db: Session, *, include_demo_kits: bool) -> None:
    """Каталоги из JSON + анкета вплетения + (опционально) демо-комплекты."""
    ensure_vsy_golova_catalog(db)
    ensure_zakaz_catalog(db)
    ensure_malishki_muzhchiny_catalog(db)
    ensure_miniatyura_catalog(db)
    ensure_narashivanie_catalog(db)
    ensure_prodazha_materiala_catalog(db)
    ensure_snjatie_ukhod_catalogs(db)
    _deactivate_legacy_inlay_4h_service(db)

    _ensure_visit_questionnaire_layout(db)

    _ensure_zakaz_products_catalog(db)
    _ensure_prodazha_materiala_products_catalog(db)

    if (not include_demo_kits) or db.scalar(select(Kit).limit(1)):
        return

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
                blanks_condition=KitBlanksCondition.NEW,
                stock_price_total=3500.0,
                discount_percent=0,
                cost_total=2800.0,
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
                blanks_condition=KitBlanksCondition.NEW,
                stock_price_total=800.0,
                discount_percent=0,
                cost_total=600.0,
                is_in_stock=True,
                is_archived=False,
            )
        )


def _ensure_demo_operational_data(db: Session) -> None:
    """
    Демо-операционные данные (1–3 записи), чтобы руками не набивать каждый раз:
    клиенты, визиты, продажа товаров, работа с товарами, расходы, периоды ЗП.

    Идемпотентно: если в таблице уже есть записи — пропускаем.
    """

    # ---- Clients ----
    if not db.scalar(select(Client).limit(1)):
        db.add_all(
            [
                Client(name="Демо клиент 1", phone="+7 900 000-00-01", is_confirmed=True),
                Client(name="Демо клиент 2", phone="+7 900 000-00-02", is_confirmed=True),
            ]
        )
        db.flush()

    c1 = db.scalar(select(Client).order_by(Client.id.asc()).limit(1))
    c2 = db.scalar(select(Client).order_by(Client.id.asc()).offset(1).limit(1)) or c1

    # ---- Payroll periods ----
    if not db.scalar(select(PayrollPeriod).limit(1)):
        now = utcnow_naive()
        cur_from = datetime(now.year, now.month, 1)
        next_month = (cur_from + timedelta(days=32)).replace(day=1)
        cur_to = next_month - timedelta(seconds=1)
        prev_to = cur_from - timedelta(seconds=1)
        prev_from = datetime(prev_to.year, prev_to.month, 1)
        db.add_all(
            [
                PayrollPeriod(
                    date_from=prev_from,
                    date_to=prev_to,
                    closed_at=now,
                    closed_by_name="seed",
                    closed_by_role="SYSTEM",
                    comment="Демо закрытый период",
                ),
                PayrollPeriod(
                    date_from=cur_from,
                    date_to=cur_from,
                    closed_at=None,
                    closed_by_name=None,
                    closed_by_role=None,
                    comment="Демо открытый период (дата «По» задаётся при закрытии)",
                ),
            ]
        )

    # Resolve demo users
    admin = db.scalar(select(User).where(User.username == "admin"))
    m1 = db.scalar(select(User).where(User.username == "master1"))
    m2 = db.scalar(select(User).where(User.username == "master2")) or m1

    # ---- Visits ----
    if not db.scalar(select(Visit).limit(1)):
        # pick any active service from catalog to attach as line
        svc = db.scalar(select(Service).where(Service.is_active.is_(True)).order_by(Service.id.asc()).limit(1))
        if svc is None:
            return
        sub = db.get(ServiceSubcategory, svc.subcategory_id)
        cat = db.get(ServiceCategory, sub.category_id) if sub else None

        v1 = Visit(
            created_by_user_id=admin.id if admin else None,
            performed_date=utcnow_naive() - timedelta(days=1),
            duration_minutes=180,
            client_id=c1.id,
            client_type=VisitClientType.NEW,
            price_type=VisitPriceType.CLIENT,
            client_age_group=None,
            kanekalon_grams=50.0,
            kudri_grams=0.0,
            mix_source=None,
            mix_complexity=None,
            mix_cost_amount=0.0,
            mix_bonus_master_id=None,
            mix_bonus_amount=0.0,
            kanekalon_price_per_gram_at_time=4.0,
            kudri_price_per_gram_at_time=8.0,
            materials_cost_total=200.0,
            amount_from_client=6000.0,
            client_discount_percent=0,
            comment="Демо визит",
            addons_total=0.0,
            addons_details_json=None,
            amortization_level=None,
            amortization_amount=0.0,
            studio_fund_amount=300.0,
            cost_total=200.0,
            profit_before_split=5800.0,
            salon_cut_pct_at_time=0.5,
            salon_profit=1740.0,
            masters_pool=4060.0,
        )
        db.add(v1)
        db.flush()
        db.add_all(
            [
                VisitMaster(visit_id=v1.id, master_id=m1.id if m1 else 1, percent=70.0),
                VisitMaster(visit_id=v1.id, master_id=m2.id if m2 else (m1.id if m1 else 1), percent=30.0),
                VisitService(
                    visit_id=v1.id,
                    service_id=svc.id,
                    details_json=None,
                    category_name=(cat.name if cat else "Категория"),
                    subcategory_name=(sub.name if sub else "Подкатегория"),
                    service_name=svc.name,
                ),
            ]
        )

    # ---- Product sales ----
    if not db.scalar(select(ProductSale).limit(1)):
        # MATERIAL sale
        ms = db.scalar(
            select(Service)
            .join(ServiceSubcategory, Service.subcategory_id == ServiceSubcategory.id)
            .join(ServiceCategory, ServiceSubcategory.category_id == ServiceCategory.id)
            .where(ServiceCategory.name == "Продажа материала", Service.is_active.is_(True))
            .order_by(Service.id.asc())
            .limit(1)
        )
        demo_creator_id = admin.id if admin else (m1.id if m1 else 1)
        demo_creator_role = (
            UserRole.ADMIN_SUPER if admin and admin.role == UserRole.ADMIN_SUPER else UserRole.MASTER
        )
        grams_demo = 30.0
        mat_kwargs: dict = {
            "material_description": "Демо: материал",
        }
        if ms and ms.retail_material_kanekalon:
            mat_kwargs["material_kanekalon_grams"] = grams_demo
        elif ms and ms.retail_material_kudri:
            mat_kwargs["material_kudri_grams"] = grams_demo
        else:
            mat_kwargs["material_grams"] = grams_demo
        ps_mat = ProductSale(
            created_by_user_id=demo_creator_id,
            performed_date=utcnow_naive(),
            client_id=c2.id,
            amount_from_client=1200,
            kind=ProductSaleKind.MATERIAL,
            material_service_id=ms.id if ms else None,
            **mat_kwargs,
        )
        db.add(ps_mat)
        db.flush()
        finalize_material_sale_fields(
            db, ps_mat, seller_user_id=demo_creator_id, active_role=demo_creator_role
        )

        # KIT sale (reduce stock like real flow)
        kit = db.scalar(select(Kit).where(Kit.sku == "DEMO-001").limit(1))
        if kit:
            pieces = 5
            kit.pieces_available = max(0, int(kit.pieces_available - pieces))
            db.add(
                ProductSale(
                    created_by_user_id=admin.id if admin else (m1.id if m1 else 1),
                    performed_date=utcnow_naive(),
                    client_id=c1.id,
                    amount_from_client=2500,
                    kind=ProductSaleKind.KIT,
                    kit_id=kit.id,
                    kit_pieces_sold=pieces,
                )
            )

    # ---- Work for inventory ----
    if not db.scalar(select(WorkForInventory).limit(1)):
        w = WorkForInventory(
            created_by_user_id=m1.id if m1 else (admin.id if admin else 1),
            kind=WorkKind.MIX,
            scope=WorkScope.IN_STOCK,
            client_id=None,
            amount_from_client=None,
            comment="Демо: смешка",
            kanekalon_grams=20.0,
            kudri_grams=10.0,
            mix_source=None,
            kanekalon_price_per_gram_at_time=4.0,
            kudri_price_per_gram_at_time=8.0,
            materials_cost_total=160.0,
            extra_costs_amount=0.0,
            cost_total_amount=160.0,
            master_profit_amount=45.0,
            studio_profit_amount=0.0,
            profit_total_amount=45.0,
            studio_share_snapshot=0.0,
            rates_snapshot_json=None,
            details_json=None,
        )
        db.add(w)
        db.flush()
        db.add(
            WorkForInventoryStaff(
                work_id=w.id,
                user_id=m1.id if m1 else 1,
                share=1.0,
                master_profit_amount=45.0,
                details_json=None,
            )
        )

    # ---- Bookings ----
    if not db.scalar(select(Booking).limit(1)):
        creator_id = admin.id if admin else (m1.id if m1 else 1)
        # 1) Sale: KIT on ORDER → multiple masters
        kit_order_master_ids = sorted(
            set(
                [
                    int(m1.id if m1 else 1),
                    int(m2.id if m2 else (m1.id if m1 else 1)),
                ]
            )
        )
        b1 = Booking(
            created_by_user_id=creator_id,
            client_id=c1.id,
            planned_date=(utcnow_naive() + timedelta(days=3)).replace(
                second=0, microsecond=0
            ),
            kind=BookingKind.PRODUCT_SALE,
            status=BookingStatus.ACTIVE,
            quoted_price_text="8000–10000",
            deposit_amount=1000,
            comment="Демо бронь: комплект на заказ",
            planned_service_id=None,
            planned_product_kind="KIT",
            details_json=json.dumps(
                {
                    "product_kind": "KIT",
                    "sale_kit_mode": "ORDER",
                    "sale_order_blanks_qty": "10",
                    "sale_order_blanks_desc": "DE/SE, омбре, материал по запросу",
                    "sale_kit_order_master_ids": ",".join([str(x) for x in kit_order_master_ids]),
                },
                ensure_ascii=False,
            ),
        )
        db.add(b1)
        db.flush()
        db.add_all(
            [
                BookingStaff(
                    booking_id=b1.id,
                    user_id=uid,
                    kind=BookingStaffKind.SALE_KIT_ORDER,
                )
                for uid in kit_order_master_ids
            ]
        )

        # 2) Sale: RUBBER on ORDER → single master
        b2 = Booking(
            created_by_user_id=creator_id,
            client_id=c2.id,
            planned_date=(utcnow_naive() + timedelta(days=5)).replace(
                second=0, microsecond=0
            ),
            kind=BookingKind.PRODUCT_SALE,
            status=BookingStatus.ACTIVE,
            quoted_price_text="1500",
            deposit_amount=None,
            comment="Демо бронь: хвост/резинка на заказ",
            planned_service_id=None,
            planned_product_kind="RUBBER",
            details_json=json.dumps(
                {
                    "product_kind": "RUBBER",
                    "sale_rubber_mode": "ORDER",
                    "sale_rubber_order_master_id": str(m1.id if m1 else 1),
                    "sale_rubber_type": "TAIL_ELASTIC",
                    "sale_rubber_attach_qty": "2",
                    "sale_rubber_desc": "Цвет чёрный, длина 50см",
                },
                ensure_ascii=False,
            ),
        )
        db.add(b2)
        db.flush()
        db.add(
            BookingStaff(
                booking_id=b2.id,
                user_id=m1.id if m1 else 1,
                kind=BookingStaffKind.SALE_RUBBER_ORDER,
            )
        )

    # ---- Expenses ----
    if not db.scalar(select(StudioExpense).limit(1)):
        sub = db.scalar(select(StudioExpenseSubcategory).order_by(StudioExpenseSubcategory.id.asc()).limit(1))
        if sub:
            db.add(
                StudioExpense(
                    created_by_user_id=admin.id if admin else (m1.id if m1 else 1),
                    created_at=utcnow_naive(),
                    date=utcnow_naive(),
                    subcategory_id=sub.id,
                    amount=500.0,
                    comment="Демо расход",
                )
            )

    db.flush()
    sync_operational_payroll_postings(db)

