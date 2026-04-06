"""
Визит с услугой «Вплетение комплекта»: разбор формы мастера, сохранение визита,
расчёт материалов/смешки/амортизации и блок комплекта в `visit_services.details_json`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import or_, select
from starlette.datastructures import UploadFile
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Client,
    Kit,
    MaterialPriceCurrent,
    MaterialType,
    AmortizationLevel,
    MixComplexity,
    MixSource,
    QuestionnaireFieldType,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    Setting,
    User,
    UserRole,
    Visit,
    VisitClientType,
    VisitKitUsage,
    VisitMaster,
    VisitPriceType,
    VisitService,
)
from app.client_validation import client_has_any_contact, strip_or_none
from app.questionnaire.answer_validate import (
    extract_questionnaire_raw_from_form,
    validate_and_coerce_answers,
)
from app.questionnaire.runtime_merge import load_merged_questionnaire_specs, merged_questionnaire_client_json
from app.questionnaire.schemas import (
    KitBlock,
    KitFromStock,
    KitOwn,
    KitOwnExtra,
    VisitServiceDetailsPayload,
    parse_visit_service_details,
)


def get_salon_cut_pct(db: Session) -> float:
    row = db.get(Setting, "salon_cut_pct")
    if not row:
        return 0.3
    try:
        return float(row.value.replace(",", "."))
    except ValueError:
        return 0.3


def _materials_cost_and_snapshot(
    db: Session,
    *,
    kanekalon_grams: float,
    kudri_grams: float,
) -> tuple[float, float | None, float | None]:
    """Считаем стоимость материалов, если указаны граммы (чекбокс «материал» не обязателен)."""
    has_grams = kanekalon_grams > 0 or kudri_grams > 0
    if not has_grams:
        return 0.0, None, None
    pk = db.get(MaterialPriceCurrent, MaterialType.KANEKALON)
    pku = db.get(MaterialPriceCurrent, MaterialType.KUDRI)
    k_price = pk.price_per_gram if pk else 0.0
    ku_price = pku.price_per_gram if pku else 0.0
    cost = kanekalon_grams * k_price + kudri_grams * ku_price
    return cost, k_price, ku_price


def _validate_stock_selection(
    db: Session,
    *,
    kit_id: int,
    use_entire: bool,
    blanks_used: int,
) -> Kit:
    """Проверка без списания (для сборки JSON)."""
    kit = db.get(Kit, kit_id)
    if not kit or kit.is_archived or not kit.is_active:
        raise ValueError("Комплект не найден или недоступен")
    avail = kit.pieces_available
    if avail <= 0:
        raise ValueError("Нет заготовок на складе по этому комплекту")
    n = avail if use_entire else blanks_used
    if n <= 0:
        raise ValueError("Укажите количество заготовок или «весь комплект»")
    if n > avail:
        raise ValueError(f"Нельзя списать больше, чем в наличии ({avail})")
    return kit


def _apply_stock_kit_usage(
    db: Session,
    *,
    kit_id: int,
    use_entire: bool,
    blanks_used: int,
) -> tuple[int, float]:
    """Списание: (pieces_used, cost_amount)."""
    kit = _validate_stock_selection(db, kit_id=kit_id, use_entire=use_entire, blanks_used=blanks_used)
    avail = kit.pieces_available
    n = avail if use_entire else blanks_used
    price = kit.stock_price_total or 0.0
    total_pieces = kit.pieces_total or 1
    cost = price * (n / total_pieces) if total_pieces else 0.0
    kit.pieces_available = avail - n
    if kit.pieces_available <= 0:
        kit.is_in_stock = False
    return n, cost


def read_visit_master_form_state(form: Any) -> tuple[list[int], dict[int, str]]:
    """Состояние блока мастеров из POST (для префилла при ошибке)."""
    raw: list[Any] = []
    if hasattr(form, "getlist"):
        raw = list(form.getlist("visit_master_on"))
    else:
        v = form.get("visit_master_on")
        if v is not None:
            raw = [v]

    def _field_str(name: str) -> str:
        v = form.get(name)
        if v is None or isinstance(v, UploadFile):
            return ""
        if isinstance(v, (bytes, bytearray)):
            return v.decode().strip()
        return str(v).strip()

    seen: set[int] = set()
    active_ids: list[int] = []
    for x in raw:
        if isinstance(x, UploadFile):
            continue
        try:
            s = x.decode().strip() if isinstance(x, (bytes, bytearray)) else str(x).strip()
            i = int(s)
        except (ValueError, AttributeError):
            continue
        if i <= 0 or i in seen:
            continue
        seen.add(i)
        active_ids.append(i)

    pct_str: dict[int, str] = {}
    for mid in active_ids:
        pct_str[mid] = _field_str(f"visit_master_pct_{mid}")
    return active_ids, pct_str


def _parse_visit_master_allocations_from_form(form: Any) -> list[tuple[int, int]]:
    """Чекбоксы `visit_master_on` + целые проценты `visit_master_pct_<id>`.

    Один отмеченный мастер: пустое поле процента считается как 100%.
    Несколько мастеров: у каждого отмеченного процент обязателен, сумма 100 (проверка ниже).
    """
    active_ids, pct_str = read_visit_master_form_state(form)
    if not active_ids:
        raise ValueError("Отметьте хотя бы одного мастера и укажите доли в процентах.")
    rows: list[tuple[int, int]] = []
    if len(active_ids) == 1:
        mid = active_ids[0]
        s = pct_str.get(mid, "").strip()
        if not s:
            p = 100
        else:
            try:
                p = int(s)
            except ValueError:
                raise ValueError("Проценты мастеров должны быть целыми числами.")
        rows.append((mid, p))
        return rows
    for mid in active_ids:
        s = pct_str.get(mid, "").strip()
        if not s:
            raise ValueError("Для каждого отмеченного мастера укажите целый процент.")
        try:
            p = int(s)
        except ValueError:
            raise ValueError("Проценты мастеров должны быть целыми числами.")
        rows.append((mid, p))
    return rows


def _resolve_visit_master_allocations(
    db: Session, allocations: list[tuple[int, int]]
) -> list[tuple[int, float]]:
    if not allocations:
        raise ValueError("Отметьте хотя бы одного мастера и укажите доли в процентах.")
    total = sum(p for _, p in allocations)
    if total != 100:
        raise ValueError("Сумма долей мастеров должна быть ровно 100%.")
    for _, p in allocations:
        if p < 0 or p > 100:
            raise ValueError("Процент каждого мастера должен быть от 0 до 100.")
    seen: set[int] = set()
    out: list[tuple[int, float]] = []
    for mid, p in allocations:
        if mid in seen:
            continue
        u = db.get(User, mid)
        if not u or not u.is_active:
            raise ValueError(f"Мастер (ID {mid}) не найден или отключён.")
        if u.role != UserRole.MASTER:
            dn = (u.display_name or u.username or "").strip() or f"ID {mid}"
            raise ValueError(f"«{dn}» не в роли мастера.")
        seen.add(mid)
        out.append((mid, float(p)))
    if len(out) != len(allocations):
        raise ValueError("Дублирование мастера в списке долей не допускается.")
    return out


def parse_kit_inlay_form(form: Any) -> KitInlayFormInput:
    """Разбор `starlette.datastructures.FormData` после `await request.form()`."""

    def g(name: str, default: str = "") -> str:
        v = form.get(name)
        if v is None:
            return default
        if isinstance(v, UploadFile):
            return default
        if isinstance(v, (bytes, bytearray)):
            return v.decode().strip()
        return str(v).strip()

    def g_int(name: str, default: int = 0) -> int:
        try:
            return int(g(name, str(default)) or default)
        except ValueError:
            return default

    def g_float(name: str, default: float = 0.0) -> float:
        try:
            return float((g(name, str(default)) or str(default)).replace(",", "."))
        except ValueError:
            return default

    def g_bool(name: str) -> bool:
        v = form.get(name)
        if v is None:
            return False
        if isinstance(v, UploadFile):
            return False
        s = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
        return s.lower() in ("on", "true", "1", "yes")

    kanekalon_grams = g_float("kanekalon_grams", 0)
    kudri_grams = g_float("kudri_grams", 0)
    grams_total = max(0.0, kanekalon_grams) + max(0.0, kudri_grams)

    mix_raw = g("mix_source", "")
    mix: MixSource | None
    if grams_total <= 0:
        mix = MixSource.NO_MIX
    else:
        mix = None
        if mix_raw:
            try:
                mix = MixSource(mix_raw)
            except ValueError:
                mix = MixSource.NO_MIX
        if mix is None:
            mix = MixSource.NO_MIX

    comp: MixComplexity | None = None
    if grams_total > 0 and mix is not None and mix != MixSource.NO_MIX:
        comp_raw = g("mix_complexity", "")
        if comp_raw:
            try:
                comp = MixComplexity(comp_raw)
            except ValueError:
                comp = None

    amort_raw = g("amortization_level", "") or "MIN"
    try:
        amort = AmortizationLevel(amort_raw)
    except ValueError:
        amort = AmortizationLevel.MIN

    stock_id = g_int("stock_kit_id", 0)
    extra_stock_id = g_int("own_extra_stock_kit_id", 0)

    ct = VisitClientType.SELF if g_bool("client_is_self") else VisitClientType.RETURNING
    try:
        pt = VisitPriceType(g("price_type", "CLIENT"))
    except ValueError:
        pt = VisitPriceType.CLIENT

    pd_raw = g("performed_date", "")
    try:
        performed_date = date.fromisoformat(pd_raw) if pd_raw else date.today()
    except ValueError:
        performed_date = date.today()

    mode_raw = (g("client_mode", "existing") or "existing").lower()
    client_mode = "draft" if mode_raw == "draft" else "existing"
    eid = g_int("existing_client_id", 0)
    existing_client_id = eid if eid > 0 else None

    return KitInlayFormInput(
        client_mode=client_mode,
        existing_client_id=existing_client_id,
        draft_name=g("draft_client_name"),
        draft_phone=g("draft_phone"),
        draft_telegram=g("draft_telegram"),
        draft_vk=g("draft_vk"),
        draft_instagram=g("draft_instagram"),
        draft_other_contact=g("draft_other_contact"),
        client_type=ct,
        price_type=pt,
        performed_date=performed_date,
        duration_minutes=g_int("duration_h", 0) * 60 + g_int("duration_m", 0),
        amount_from_client=g_float("amount_from_client", 0),
        kanekalon_grams=kanekalon_grams,
        kudri_grams=kudri_grams,
        mix_source=mix,
        mix_complexity=comp,
        amortization_level=amort,
        service_id=g_int("service_id", 0),
        kit_kind=g("kit_kind", "STOCK").upper(),
        stock_kit_id=stock_id if stock_id else None,
        stock_use_entire=g_bool("stock_use_entire"),
        stock_blanks_used=g_int("stock_blanks_used", 0),
        # В текущем шаге анкеты «Новый комплект» недоступен, поэтому поля не парсим.
        new_title="",
        new_description=None,
        new_blanks_total=0,
        new_sku=None,
        new_made_by_self=False,
        new_notes=None,
        own_origin=g("own_origin") or None,
        own_correction=g_bool("own_correction"),
        own_extra_blanks=g_bool("own_extra_blanks"),
        own_extra_stock_kit_id=extra_stock_id if extra_stock_id else None,
        own_extra_stock_use_entire=g_bool("own_extra_stock_use_entire"),
        own_extra_stock_blanks_used=g_int("own_extra_stock_blanks_used", 0),
        visit_master_allocations=_parse_visit_master_allocations_from_form(form),
        questionnaire_raw=extract_questionnaire_raw_from_form(form),
    )


@dataclass
class KitInlayFormInput:
    client_mode: str  # "existing" | "draft"
    existing_client_id: int | None
    draft_name: str
    draft_phone: str
    draft_telegram: str
    draft_vk: str
    draft_instagram: str
    draft_other_contact: str
    client_type: VisitClientType
    price_type: VisitPriceType
    performed_date: date
    duration_minutes: int
    amount_from_client: float
    kanekalon_grams: float
    kudri_grams: float
    mix_source: MixSource | None
    mix_complexity: MixComplexity | None
    amortization_level: AmortizationLevel | None
    service_id: int
    kit_kind: str
    # STOCK
    stock_kit_id: int | None
    stock_use_entire: bool
    stock_blanks_used: int
    # NEW
    new_title: str
    new_description: str | None
    new_blanks_total: int
    new_sku: str | None
    new_made_by_self: bool
    new_notes: str | None
    # OWN
    own_origin: str | None
    own_correction: bool
    own_extra_blanks: bool
    own_extra_stock_kit_id: int | None
    own_extra_stock_use_entire: bool
    own_extra_stock_blanks_used: int
    visit_master_allocations: list[tuple[int, int]]
    questionnaire_raw: dict[str, str]


def build_payload_from_input(inp: KitInlayFormInput, db: Session) -> VisitServiceDetailsPayload:
    kind = inp.kit_kind.upper()
    if kind == "STOCK":
        if not inp.stock_kit_id:
            raise ValueError("Выберите комплект из наличия")
        kit_row = _validate_stock_selection(
            db,
            kit_id=inp.stock_kit_id,
            use_entire=inp.stock_use_entire,
            blanks_used=inp.stock_blanks_used,
        )
        kit_block = KitBlock(
            kind="STOCK",
            from_stock=KitFromStock(
                sku=kit_row.sku,
                blanks_used=0 if inp.stock_use_entire else inp.stock_blanks_used,
                use_entire_kit=inp.stock_use_entire,
            ),
            new_kit=None,
            own=None,
        )
    elif kind == "NEW":
        # В текущем шаге анкеты «Новый комплект» недоступен (используй только из наличия или свой).
        raise ValueError("Режим «Новый комплект» временно отключён. Выберите «Из наличия» или «Свой».")
    elif kind == "OWN":
        if inp.own_origin not in ("STUDIO", "FOREIGN"):
            raise ValueError("Укажите происхождение своего комплекта")
        extra: KitOwnExtra | None = None
        if inp.own_extra_blanks:
            if not inp.own_extra_stock_kit_id:
                raise ValueError("Выберите комплект для доп. заготовок")
            ek = _validate_stock_selection(
                db,
                kit_id=inp.own_extra_stock_kit_id,
                use_entire=inp.own_extra_stock_use_entire,
                blanks_used=inp.own_extra_stock_blanks_used,
            )
            extra = KitOwnExtra(
                source="STOCK",
                from_stock=KitFromStock(
                    sku=ek.sku,
                    blanks_used=0 if inp.own_extra_stock_use_entire else inp.own_extra_stock_blanks_used,
                    use_entire_kit=inp.own_extra_stock_use_entire,
                ),
                new_kit=None,
            )
        kit_block = KitBlock(
            kind="OWN",
            from_stock=None,
            new_kit=None,
            own=KitOwn(
                origin=inp.own_origin,  # type: ignore[arg-type]
                correction=inp.own_correction,
                extra_blanks=inp.own_extra_blanks,
                extra=extra,
            ),
        )
    else:
        raise ValueError("Неверный тип комплекта")

    specs = load_merged_questionnaire_specs(db, inp.service_id)
    answers, q_errors = validate_and_coerce_answers(inp.questionnaire_raw, specs)
    if q_errors:
        raise ValueError("; ".join(q_errors))

    answer_labels = {spec.field_key: spec.label for spec in specs if spec.field_key in answers}
    answer_display: dict[str, str] = {}
    for spec in specs:
        fk = spec.field_key
        if fk not in answers:
            continue
        v = answers[fk]
        if spec.field_type == QuestionnaireFieldType.SELECT and isinstance(v, str):
            opt_lbl = next((o["label"] for o in spec.options if o.get("value") == v), None)
            answer_display[fk] = opt_lbl if opt_lbl is not None else v
        elif isinstance(v, bool):
            answer_display[fk] = "Да" if v else "Нет"
        else:
            answer_display[fk] = str(v)

    return parse_visit_service_details(
        {
            "service_fields": {},
            "kit": kit_block.model_dump(mode="json"),
            "answers": answers,
            "answer_labels": answer_labels,
            "answer_display": answer_display,
        }
    )


def save_kit_inlay_visit(
    db: Session,
    master_id: int,
    inp: KitInlayFormInput,
    *,
    created_by_label: str | None = None,
) -> Visit:
    """Визит с выбранным клиентом или новым черновиком; услуга, склад STOCK, расчёт."""
    if inp.service_id <= 0:
        raise ValueError("Выберите услугу")

    master_rows = _resolve_visit_master_allocations(db, inp.visit_master_allocations)

    service = db.scalar(
        select(Service)
        .options(selectinload(Service.subcategory).selectinload(ServiceSubcategory.category))
        .where(Service.id == inp.service_id, Service.is_active.is_(True))
    )
    if not service or not service.subcategory or not service.subcategory.category:
        raise ValueError("Услуга не найдена")
    if not service.subcategory.category.include_in_visit:
        raise ValueError("Эта позиция недоступна для выбора в визите")

    payload = build_payload_from_input(inp, db)

    mat_cost, k_snap, ku_snap = _materials_cost_and_snapshot(
        db,
        kanekalon_grams=inp.kanekalon_grams,
        kudri_grams=inp.kudri_grams,
    )
    salon_pct = get_salon_cut_pct(db)

    kit_cost_total = 0.0
    usages: list[tuple[int, int, float]] = []

    kind = inp.kit_kind.upper()
    if kind == "STOCK" and inp.stock_kit_id:
        n, cost = _apply_stock_kit_usage(
            db,
            kit_id=inp.stock_kit_id,
            use_entire=inp.stock_use_entire,
            blanks_used=inp.stock_blanks_used,
        )
        usages.append((inp.stock_kit_id, n, cost))
        kit_cost_total += cost
    if kind == "OWN" and inp.own_extra_blanks and inp.own_extra_stock_kit_id:
        n, cost = _apply_stock_kit_usage(
            db,
            kit_id=inp.own_extra_stock_kit_id,
            use_entire=inp.own_extra_stock_use_entire,
            blanks_used=inp.own_extra_stock_blanks_used,
        )
        usages.append((inp.own_extra_stock_kit_id, n, cost))
        kit_cost_total += cost

    # Доп. услуги к визиту: пока 0 (позже — сумма и детализация)
    addons = 0.0

    grams_total = max(0.0, inp.kanekalon_grams) + max(0.0, inp.kudri_grams)
    mix_cost = 0.0
    mix_bonus_amount = 0.0
    mix_bonus_master_id = None
    if inp.mix_source and inp.mix_source != MixSource.NO_MIX:
        if inp.mix_complexity is None:
            raise ValueError("Укажите сложность смешки")
        coef = {"SIMPLE": 1.0, "MEDIUM": 1.5, "HARD": 2.0}[inp.mix_complexity.value]
        mix_cost = grams_total * coef
        if inp.mix_source == MixSource.SELF_MIXED:
            mix_bonus_amount = mix_cost
            mix_bonus_master_id = master_id

    amort_amount = 0.0
    if inp.amortization_level is not None:
        amort_amount = {"MIN": 100.0, "MID": 200.0, "MAX": 500.0}[inp.amortization_level.value]

    # Расходы до распределения
    cost_total = mat_cost + kit_cost_total + addons + mix_cost + amort_amount
    profit_before = inp.amount_from_client - cost_total
    salon_profit = profit_before * salon_pct
    masters_pool = profit_before - salon_profit

    if inp.client_mode == "draft":
        if not inp.draft_name.strip():
            raise ValueError("Укажите имя клиента для черновика.")
        if not client_has_any_contact(
            inp.draft_phone,
            inp.draft_telegram,
            inp.draft_vk,
            inp.draft_instagram,
            inp.draft_other_contact,
        ):
            raise ValueError("Для черновика нужен хотя бы один контакт (телефон или соцсеть).")
        client = Client(
            name=inp.draft_name.strip()[:200],
            phone=strip_or_none(inp.draft_phone, 30),
            telegram=strip_or_none(inp.draft_telegram, 100),
            vk=strip_or_none(inp.draft_vk, 120),
            instagram=strip_or_none(inp.draft_instagram, 120),
            other_contact=strip_or_none(inp.draft_other_contact, 200),
            comment=None,
            is_confirmed=False,
            created_by_label=created_by_label,
        )
        db.add(client)
        db.flush()
    else:
        if not inp.existing_client_id:
            raise ValueError("Найдите и выберите клиента из списка или переключитесь на «Новый черновик».")
        client = db.get(Client, inp.existing_client_id)
        if client is None:
            raise ValueError("Клиент не найден.")

    performed_dt = datetime.combine(inp.performed_date, datetime.min.time())

    visit = Visit(
        created_by_user_id=master_id,
        performed_date=performed_dt,
        duration_minutes=max(0, inp.duration_minutes),
        client_id=client.id,
        client_type=inp.client_type,
        price_type=inp.price_type,
        client_age_group=client.age_group,
        kanekalon_grams=inp.kanekalon_grams,
        kudri_grams=inp.kudri_grams,
        mix_source=inp.mix_source,
        mix_complexity=inp.mix_complexity,
        mix_cost_amount=mix_cost,
        mix_bonus_master_id=mix_bonus_master_id,
        mix_bonus_amount=mix_bonus_amount,
        kanekalon_price_per_gram_at_time=k_snap,
        kudri_price_per_gram_at_time=ku_snap,
        materials_cost_total=mat_cost,
        amount_from_client=inp.amount_from_client,
        addons_total=addons,
        addons_details_json=None,
        amortization_level=inp.amortization_level,
        amortization_amount=amort_amount,
        studio_fund_amount=amort_amount,
        cost_total=cost_total,
        profit_before_split=profit_before,
        salon_cut_pct_at_time=salon_pct,
        salon_profit=salon_profit,
        masters_pool=masters_pool,
        comment=None,
    )
    db.add(visit)
    db.flush()

    for mid, pct in master_rows:
        db.add(VisitMaster(visit_id=visit.id, master_id=mid, percent=pct))

    details = payload.model_dump(mode="json")
    db.add(
        VisitService(
            visit_id=visit.id,
            service_id=service.id,
            details_json=json.dumps(details, ensure_ascii=False),
            category_name=service.subcategory.category.name,
            subcategory_name=service.subcategory.name,
            service_name=service.name,
        )
    )

    for kid, pieces, camount in usages:
        db.add(
            VisitKitUsage(
                visit_id=visit.id,
                kit_id=kid,
                pieces_used=pieces,
                cost_amount=camount,
                note=None,
            )
        )

    db.commit()
    db.refresh(visit)
    return visit


def list_kit_inlay_services(db: Session) -> list[Service]:
    q = (
        select(Service)
        .join(Service.subcategory)
        .join(ServiceSubcategory.category)
        .where(
            ServiceSubcategory.name == "Вплетение комплекта",
            Service.is_active.is_(True),
            ServiceCategory.include_in_visit.is_(True),
        )
        .order_by(Service.name.asc())
    )
    return list(db.scalars(q).all())


def list_kit_inlay_services_catalog(db: Session) -> list[dict[str, Any]]:
    """
    Упрощенный каталог для UI: категория -> подкатегория -> услуга.

    Сейчас страница мастера поддерживает только услугу «Вплетение комплекта»,
    поэтому каталог тоже ограничен этим набором сервисов.
    """

    def _opt_f(v: float | None) -> float | None:
        return None if v is None else float(v)

    services = list_kit_inlay_services(db)
    cats: dict[int, dict[str, Any]] = {}
    for s in services:
        sub = getattr(s, "subcategory", None)
        cat = getattr(sub, "category", None) if sub else None
        if not sub or not cat:
            continue

        c_id = int(cat.id)
        sc_id = int(sub.id)

        if c_id not in cats:
            cats[c_id] = {"id": c_id, "name": cat.name, "subcategories": {}}

        subs = cats[c_id]["subcategories"]
        if sc_id not in subs:
            subs[sc_id] = {"id": sc_id, "name": sub.name, "services": []}

        subs[sc_id]["services"].append(
            {
                "id": int(s.id),
                "name": s.name,
                "price_junior_from": _opt_f(s.price_junior_from),
                "price_junior_to": _opt_f(s.price_junior_to),
                "price_middle_from": _opt_f(s.price_middle_from),
                "price_middle_to": _opt_f(s.price_middle_to),
                "price_senior_from": _opt_f(s.price_senior_from),
                "price_senior_to": _opt_f(s.price_senior_to),
                "questionnaire_fields": merged_questionnaire_client_json(db, int(s.id)),
            }
        )

    # Convert dict -> list, keep stable ordering.
    out: list[dict[str, Any]] = []
    for _, c in sorted(cats.items(), key=lambda x: x[1]["name"]):
        subs_out: list[dict[str, Any]] = []
        for _, sc in sorted(c["subcategories"].items(), key=lambda x: x[1]["name"]):
            sc["services"] = sorted(sc["services"], key=lambda x: x["name"])
            subs_out.append(sc)
        out.append({"id": c["id"], "name": c["name"], "subcategories": subs_out})
    return out


def list_kits_for_stock(db: Session) -> list[Kit]:
    return list(
        db.scalars(
            select(Kit)
            .where(
                Kit.is_archived.is_(False),
                Kit.is_active.is_(True),
                Kit.pieces_available > 0,
            )
            .order_by(Kit.sku.asc())
        ).all()
    )


def kit_reserved_for_visit_label(kit: Kit) -> str | None:
    """Краткое «для кого» резерв в анкете визита (без даты и автора)."""
    if not kit.reserved_at:
        return None
    parts: list[str] = []
    if kit.reserved_for_client:
        n = (kit.reserved_for_client.name or "").strip() or "—"
        parts.append(f"клиент «{n}»")
    if kit.reserved_for_user:
        dn = (kit.reserved_for_user.display_name or "").strip() or "—"
        parts.append(f"сотрудник «{dn}»")
    if not parts:
        return "цель резерва в карточке не заполнена"
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} и {parts[1]}"


def kit_reserve_hint_by_id(db: Session, kit_id: int | None) -> str | None:
    if not kit_id:
        return None
    k = db.scalar(
        select(Kit)
        .options(
            selectinload(Kit.reserved_for_client),
            selectinload(Kit.reserved_for_user),
        )
        .where(Kit.id == kit_id)
    )
    if not k:
        return None
    return kit_reserved_for_visit_label(k)


def suggest_kits_for_stock(db: Session, q: str, *, limit: int = 30) -> list[dict[str, Any]]:
    """Подсказки для мастера: комплекты в наличии, фильтр по артикулу или названию."""
    needle = (q or "").strip()
    stmt = (
        select(Kit)
        .where(
            Kit.is_archived.is_(False),
            Kit.is_active.is_(True),
            Kit.pieces_available > 0,
        )
        .options(
            selectinload(Kit.reserved_for_client),
            selectinload(Kit.reserved_for_user),
        )
    )
    if needle:
        stmt = stmt.where(
            or_(Kit.sku.ilike(f"%{needle}%"), Kit.title.ilike(f"%{needle}%"))
        )
    stmt = stmt.order_by(Kit.sku.asc()).limit(limit)
    rows = list(db.scalars(stmt).all())
    out: list[dict[str, Any]] = []
    for k in rows:
        res_label = kit_reserved_for_visit_label(k)
        out.append(
            {
                "id": k.id,
                "sku": k.sku,
                "title": k.title,
                "pieces_available": k.pieces_available,
                "is_reserved": bool(k.reserved_at),
                "reserved_for_label": res_label,
            }
        )
    return out
