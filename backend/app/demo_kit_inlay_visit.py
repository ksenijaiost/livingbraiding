"""
Демо-поток: визит с услугой «Вплетение комплекта» + блок kit в `visit_services.details_json`.

Дальше этот код можно заменить на полноценную анкету и общий сервис расчётов.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
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
    Service,
    ServiceSubcategory,
    Setting,
    Visit,
    VisitClientType,
    VisitKitUsage,
    VisitMaster,
    VisitPriceType,
    VisitService,
)
from app.questionnaire.schemas import (
    KitBlock,
    KitFromStock,
    KitNew,
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

    mix_raw = g("mix_source", "")
    mix: MixSource | None = None
    if mix_raw:
        try:
            mix = MixSource(mix_raw)
        except ValueError:
            mix = None

    comp_raw = g("mix_complexity", "")
    comp: MixComplexity | None = None
    if comp_raw:
        try:
            comp = MixComplexity(comp_raw)
        except ValueError:
            comp = None

    amort_raw = g("amortization_level", "")
    amort: AmortizationLevel | None = None
    if amort_raw:
        try:
            amort = AmortizationLevel(amort_raw)
        except ValueError:
            amort = None

    stock_id = g_int("stock_kit_id", 0)
    extra_stock_id = g_int("own_extra_stock_kit_id", 0)

    try:
        ct = VisitClientType(g("client_type", "NEW"))
    except ValueError:
        ct = VisitClientType.NEW
    try:
        pt = VisitPriceType(g("price_type", "CLIENT"))
    except ValueError:
        pt = VisitPriceType.CLIENT

    pd_raw = g("performed_date", "")
    try:
        performed_date = date.fromisoformat(pd_raw) if pd_raw else date.today()
    except ValueError:
        performed_date = date.today()

    return KitInlayFormInput(
        client_name=g("client_name"),
        client_contact=g("client_contact") or None,
        client_type=ct,
        price_type=pt,
        performed_date=performed_date,
        duration_minutes=g_int("duration_h", 0) * 60 + g_int("duration_m", 0),
        amount_from_client=g_float("amount_from_client", 0),
        kanekalon_grams=g_float("kanekalon_grams", 0),
        kudri_grams=g_float("kudri_grams", 0),
        mix_source=mix,
        mix_complexity=comp,
        amortization_level=amort,
        service_id=g_int("service_id", 0),
        kit_kind=g("kit_kind", "STOCK").upper(),
        stock_kit_id=stock_id if stock_id else None,
        stock_use_entire=g_bool("stock_use_entire"),
        stock_blanks_used=g_int("stock_blanks_used", 0),
        new_title=g("new_title"),
        new_description=g("new_description") or None,
        new_blanks_total=g_int("new_blanks_total", 0),
        new_sku=g("new_sku") or None,
        new_made_by_self=g_bool("new_made_by_self"),
        new_notes=g("new_notes") or None,
        own_origin=g("own_origin") or None,
        own_correction=g_bool("own_correction"),
        own_extra_blanks=g_bool("own_extra_blanks"),
        own_extra_source=g("own_extra_source") or None,
        own_extra_stock_kit_id=extra_stock_id if extra_stock_id else None,
        own_extra_stock_use_entire=g_bool("own_extra_stock_use_entire"),
        own_extra_stock_blanks_used=g_int("own_extra_stock_blanks_used", 0),
        own_extra_new_title=g("own_extra_new_title"),
        own_extra_new_description=g("own_extra_new_description") or None,
        own_extra_new_blanks_total=g_int("own_extra_new_blanks_total", 0),
        own_extra_new_sku=g("own_extra_new_sku") or None,
        own_extra_new_made_by_self=g_bool("own_extra_new_made_by_self"),
        own_extra_new_notes=g("own_extra_new_notes") or None,
        bases_count=g_int("bases_count", 0),
        blanks_count=g_int("blanks_count", 0),
        service_comment=g("service_comment") or None,
    )


@dataclass
class KitInlayFormInput:
    client_name: str
    client_contact: str | None
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
    own_extra_source: str | None
    own_extra_stock_kit_id: int | None
    own_extra_stock_use_entire: bool
    own_extra_stock_blanks_used: int
    own_extra_new_title: str
    own_extra_new_description: str | None
    own_extra_new_blanks_total: int
    own_extra_new_sku: str | None
    own_extra_new_made_by_self: bool
    own_extra_new_notes: str | None
    # service fields
    bases_count: int
    blanks_count: int
    service_comment: str | None


def build_payload_from_input(inp: KitInlayFormInput, db: Session) -> VisitServiceDetailsPayload:
    service_fields = {
        "bases_count": inp.bases_count,
        "blanks_count": inp.blanks_count,
        "service_comment": inp.service_comment or None,
    }
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
        if not inp.new_title.strip():
            raise ValueError("Укажите название нового комплекта")
        kit_block = KitBlock(
            kind="NEW",
            from_stock=None,
            new_kit=KitNew(
                title=inp.new_title.strip(),
                description=(inp.new_description or "").strip() or None,
                blanks_total=inp.new_blanks_total,
                sku=(inp.new_sku or "").strip() or None,
                made_by_self=inp.new_made_by_self,
                notes=(inp.new_notes or "").strip() or None,
            ),
            own=None,
        )
    elif kind == "OWN":
        if inp.own_origin not in ("STUDIO", "FOREIGN"):
            raise ValueError("Укажите происхождение своего комплекта")
        extra: KitOwnExtra | None = None
        if inp.own_extra_blanks:
            if inp.own_extra_source == "STOCK":
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
            elif inp.own_extra_source == "NEW":
                if not inp.own_extra_new_title.strip():
                    raise ValueError("Название для новых доп. заготовок")
                extra = KitOwnExtra(
                    source="NEW",
                    from_stock=None,
                    new_kit=KitNew(
                        title=inp.own_extra_new_title.strip(),
                        description=(inp.own_extra_new_description or "").strip() or None,
                        blanks_total=inp.own_extra_new_blanks_total,
                        sku=(inp.own_extra_new_sku or "").strip() or None,
                        made_by_self=inp.own_extra_new_made_by_self,
                        notes=(inp.own_extra_new_notes or "").strip() or None,
                    ),
                )
            else:
                raise ValueError("Укажите источник доп. заготовок")
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

    return parse_visit_service_details(
        {
            "service_fields": service_fields,
            "kit": kit_block.model_dump(mode="json"),
        }
    )


def save_kit_inlay_visit(db: Session, master_id: int, inp: KitInlayFormInput) -> Visit:
    """Создаёт клиента, визит, услугу, списания со склада (STOCK), считает демо-профит."""
    if not inp.client_name.strip():
        raise ValueError("Укажите имя клиента")
    if inp.service_id <= 0:
        raise ValueError("Выберите услугу")

    payload = build_payload_from_input(inp, db)

    service = db.scalar(
        select(Service)
        .options(selectinload(Service.subcategory).selectinload(ServiceSubcategory.category))
        .where(Service.id == inp.service_id, Service.is_active.is_(True))
    )
    if not service or not service.subcategory or not service.subcategory.category:
        raise ValueError("Услуга не найдена")

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
    if kind == "OWN" and inp.own_extra_blanks and inp.own_extra_source == "STOCK" and inp.own_extra_stock_kit_id:
        n, cost = _apply_stock_kit_usage(
            db,
            kit_id=inp.own_extra_stock_kit_id,
            use_entire=inp.own_extra_stock_use_entire,
            blanks_used=inp.own_extra_stock_blanks_used,
        )
        usages.append((inp.own_extra_stock_kit_id, n, cost))
        kit_cost_total += cost

    # Addons: demo пока 0 (в реальной анкете будет сумма + строка)
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

    client = Client(
        name=inp.client_name.strip(),
        phone=(inp.client_contact or "").strip() or None,
        comment=None,
        is_confirmed=True,
    )
    db.add(client)
    db.flush()

    performed_dt = datetime.combine(inp.performed_date, datetime.min.time())

    visit = Visit(
        performed_date=performed_dt,
        duration_minutes=max(0, inp.duration_minutes),
        client_id=client.id,
        client_type=inp.client_type,
        price_type=inp.price_type,
        client_age_group=None,
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

    db.add(VisitMaster(visit_id=visit.id, master_id=master_id, percent=100.0))

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
        )
        .order_by(Service.name.asc())
    )
    return list(db.scalars(q).all())


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
