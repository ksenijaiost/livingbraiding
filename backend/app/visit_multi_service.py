"""Визит с несколькими услугами: расчёт строк, сохранение, агрегаты на Visit."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import UploadFile

from app.client_payment import parse_client_payment_kind
from app.work_products_compute import compute_correction_extra_costs
from app.client_validation import client_has_any_contact, strip_or_none
from app.db.models import (
    AmortizationLevel,
    Client,
    ClientPaymentKind,
    MixComplexity,
    MixSource,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    User,
    UserRole,
    Visit,
    VisitClientType,
    VisitKitUsage,
    VisitMaster,
    VisitMastersScope,
    VisitPriceType,
    VisitAuditLog,
    VisitService,
    VisitServiceMaster,
)
from app.audit import diff_fields, write_audit_rows
from app.forms_parse import parse_bool, parse_date_iso, parse_float
from app.kit_inlay_visit import (
    AMORTIZATION_LEVEL_RUBLES,
    KitInlayFormInput,
    StockKitLineInput,
    _apply_stock_kit_usage,
    estimate_stock_kit_usage,
    _build_kit_block_from_input,
    _materials_cost_and_snapshot,
    _parse_optional_nonneg_int,
    _parse_stock_breakdown_json,
    _parse_stock_kit_lines_from_form,
    _parse_visit_client_discount_percent,
    _parse_visit_master_allocations_from_form,
    _resolve_visit_master_allocations,
    build_payload_from_input,
    get_salon_cut_pct,
    read_visit_master_form_state,
    service_requires_kit_block,
)
from app.mix_rates import mix_complexity_rate_for
from app.payroll_fund import (
    PayrollFundSourceKind,
    post_visit_accruals,
    post_visit_service_accruals,
    replace_visit_service_accruals,
    storno_source_accruals,
)
from app.time_utils import utcnow_naive
from app.visit_edit_policy import ensure_event_date_in_open_payroll_period
from app.visit_stock import visit_service_revert_stock
from app.questionnaire.answer_validate import (
    extract_line_questionnaire_raw_from_form,
    extract_questionnaire_raw_from_form,
)
from app.thermo_visit import parse_thermo_from_form, persist_new_thermo_template_if_needed
from app.user_roles import user_has_role


_LINE_KEY_RE = re.compile(r"^line_(\d+)_")


def _line_service_id_from_form(form: Any, idx: int) -> int:
    if idx == 0:
        raw = form.get("service_id") or form.get("line_0_service_id")
    else:
        raw = form.get(f"line_{idx}_service_id")
    if raw is None or isinstance(raw, UploadFile):
        return 0
    s = raw.decode().strip() if isinstance(raw, (bytes, bytearray)) else str(raw).strip()
    if not s:
        return 0
    try:
        return int(parse_float(s, field_name="service_id"))
    except ValueError:
        return 0


def form_uses_multi_service_lines(form: Any) -> bool:
    """Вторая и далее услуги: только если указан service_id строки (не скрытые line_1_* заглушки)."""
    for key in form.keys():
        if not isinstance(key, str):
            continue
        m = _LINE_KEY_RE.match(key)
        if not m:
            continue
        idx = int(m.group(1))
        if idx >= 1 and _line_service_id_from_form(form, idx) > 0:
            return True
    return False


@dataclass
class VisitServiceLineInput:
    service_id: int
    amount_from_client: float
    client_discount_percent: int
    kanekalon_grams: float
    kudri_grams: float
    mix_source: MixSource | None
    mix_complexity: MixComplexity | None
    mix_bonus_master_id: int | None
    amortization_level: AmortizationLevel | None
    kit_kind: str
    stock_kit_lines: list[StockKitLineInput] = field(default_factory=list)
    kit_paid_separately: bool = False
    own_origin: str | None = None
    own_correction: bool = False
    own_extra_blanks: bool = False
    own_extra_stock_kit_id: int | None = None
    own_extra_stock_kit_lines: list[StockKitLineInput] = field(default_factory=list)
    own_extra_stock_use_entire: bool = False
    own_extra_stock_blanks_used: int = 0
    own_extra_stock_usage_by_key: dict[str, int] | None = None
    own_corr_trim_qty: int = 0
    own_corr_hourly_hours: float = 0.0
    own_corr_kit_description: str = ""
    own_corr_kit_blanks_count: int | None = None
    own_corr_wash: bool = False
    own_corr_circle: bool = False
    own_corr_steam: bool = False
    own_corr_use_custom_amount: bool = False
    own_corr_custom_amount: float = 0.0
    own_corr_client_payment_kind: ClientPaymentKind = ClientPaymentKind.CASH
    service_master_allocations: list[tuple[int, int]] = field(default_factory=list)
    questionnaire_raw: dict[str, str] = field(default_factory=dict)
    addon_sales_amount: float = 0.0
    addon_sales_description: str = ""
    thermo_parsed: Any = None
    started_at: datetime | None = None
    comment: str | None = None
    sort_order: int = 0
    visit_service_id: int | None = None
    client_payment_kind: ClientPaymentKind = ClientPaymentKind.CASH


def effective_amount_from_client(line: VisitServiceLineInput) -> float:
    """Сумма с клиента: основная услуга + доплата за коррекцию при «Своей сумме»."""
    base = float(line.amount_from_client or 0)
    if line.own_correction and line.own_corr_use_custom_amount:
        return base + float(line.own_corr_custom_amount or 0)
    return base


@dataclass
class VisitHeaderInput:
    client_mode: str
    existing_client_id: int | None
    draft_name: str
    draft_phone: str
    draft_telegram: str
    draft_vk: str
    draft_instagram: str
    draft_other_contact: str
    client_type: VisitClientType
    performed_date: date
    duration_minutes: int
    masters_scope: VisitMastersScope
    same_master_shares_all_services: bool
    visit_master_allocations: list[tuple[int, int]]
    booking_id: int | None = None


@dataclass
class MultiServiceVisitInput:
    header: VisitHeaderInput
    lines: list[VisitServiceLineInput]


@dataclass
class VisitServiceLineComputed:
    amount_from_client: float
    client_payment_kind: ClientPaymentKind
    client_discount_percent: int
    kanekalon_grams: float
    kudri_grams: float
    mix_source: MixSource | None
    mix_complexity: MixComplexity | None
    mix_cost_amount: float
    mix_bonus_master_id: int | None
    mix_bonus_amount: float
    kanekalon_price_per_gram_at_time: float | None
    kudri_price_per_gram_at_time: float | None
    materials_cost_total: float
    addons_total: float
    addons_details_json: str | None
    amortization_level: AmortizationLevel | None
    amortization_amount: float
    studio_fund_amount: float
    cost_total: float
    profit_before_split: float
    salon_cut_pct_at_time: float
    salon_profit: float
    masters_pool: float
    kit_paid_separately: bool
    kit_usages: list[tuple[int, int, float, dict[str, int] | None]]


def _validate_mix_bonus_master(db: Session, master_id: int | None) -> None:
    if master_id is None:
        return
    u = db.get(User, master_id)
    if not u or not u.is_active:
        raise ValueError("Мастер для бонуса смешки не найден или отключён.")
    if not user_has_role(db, master_id, UserRole.MASTER):
        raise ValueError("Бонус смешки может получить только активный мастер.")


def _line_kit_inlay_adapter(line: VisitServiceLineInput, header: VisitHeaderInput) -> KitInlayFormInput:
    return KitInlayFormInput(
        client_mode=header.client_mode,
        existing_client_id=header.existing_client_id,
        draft_name=header.draft_name,
        draft_phone=header.draft_phone,
        draft_telegram=header.draft_telegram,
        draft_vk=header.draft_vk,
        draft_instagram=header.draft_instagram,
        draft_other_contact=header.draft_other_contact,
        client_type=header.client_type,
        client_discount_percent=line.client_discount_percent,
        performed_date=header.performed_date,
        duration_minutes=header.duration_minutes,
        amount_from_client=line.amount_from_client,
        kanekalon_grams=line.kanekalon_grams,
        kudri_grams=line.kudri_grams,
        mix_source=line.mix_source,
        mix_complexity=line.mix_complexity,
        amortization_level=line.amortization_level,
        service_id=line.service_id,
        kit_kind=line.kit_kind,
        stock_kit_id=line.stock_kit_lines[0].kit_id if line.stock_kit_lines else None,
        stock_use_entire=line.stock_kit_lines[0].use_entire if line.stock_kit_lines else False,
        stock_blanks_used=line.stock_kit_lines[0].blanks_used if line.stock_kit_lines else 0,
        stock_usage_by_key=line.stock_kit_lines[0].usage_by_key if line.stock_kit_lines else None,
        stock_kit_lines=line.stock_kit_lines,
        kit_paid_separately=line.kit_paid_separately,
        new_title="",
        new_description=None,
        new_blanks_total=0,
        new_sku=None,
        new_made_by_self=False,
        new_notes=None,
        own_origin=line.own_origin,
        own_correction=line.own_correction,
        own_extra_blanks=line.own_extra_blanks,
        own_extra_stock_kit_id=line.own_extra_stock_kit_id,
        own_extra_stock_kit_lines=line.own_extra_stock_kit_lines,
        own_extra_stock_use_entire=line.own_extra_stock_use_entire,
        own_extra_stock_blanks_used=line.own_extra_stock_blanks_used,
        own_extra_stock_usage_by_key=line.own_extra_stock_usage_by_key,
        own_corr_trim_qty=line.own_corr_trim_qty,
        own_corr_hourly_hours=line.own_corr_hourly_hours,
        own_corr_kit_description=line.own_corr_kit_description,
        own_corr_kit_blanks_count=line.own_corr_kit_blanks_count,
        own_corr_wash=line.own_corr_wash,
        own_corr_circle=line.own_corr_circle,
        own_corr_steam=line.own_corr_steam,
        own_corr_use_custom_amount=line.own_corr_use_custom_amount,
        own_corr_custom_amount=line.own_corr_custom_amount,
        own_corr_client_payment_kind=line.own_corr_client_payment_kind,
        visit_master_allocations=line.service_master_allocations,
        questionnaire_raw=line.questionnaire_raw,
        addon_sales_amount=line.addon_sales_amount,
        addon_sales_description=line.addon_sales_description,
        thermo_parsed=line.thermo_parsed,
    )


def compute_visit_service_line(
    db: Session,
    line: VisitServiceLineInput,
    header: VisitHeaderInput,
    *,
    default_mix_bonus_master_id: int | None = None,
    apply_kit_stock: bool = True,
) -> VisitServiceLineComputed:
    if line.service_id <= 0:
        raise ValueError("Выберите услугу")

    service = db.scalar(
        select(Service)
        .options(selectinload(Service.subcategory).selectinload(ServiceSubcategory.category))
        .where(Service.id == line.service_id, Service.is_active.is_(True))
    )
    if not service or not service.subcategory or not service.subcategory.category:
        raise ValueError("Услуга не найдена")
    if (service.subcategory.category.name or "").strip() in ("Заказ", "Продажа материала"):
        raise ValueError("Эта позиция недоступна для выбора в визите")

    mat_cost, k_snap, ku_snap = _materials_cost_and_snapshot(
        db,
        kanekalon_grams=line.kanekalon_grams,
        kudri_grams=line.kudri_grams,
    )
    salon_pct = get_salon_cut_pct(db, default_mix_bonus_master_id)

    kit_cost_total = 0.0
    usages: list[tuple[int, int, float, dict[str, int] | None]] = []
    kit_studio_fund = 0.0

    if service_requires_kit_block(service):
        kinp = _line_kit_inlay_adapter(line, header)
        kind = line.kit_kind.upper()
        exclude_main_stock_cost = kind == "STOCK" and bool(line.kit_paid_separately) and bool(line.stock_kit_lines)
        stock_fn = _apply_stock_kit_usage if apply_kit_stock else estimate_stock_kit_usage
        if kind == "STOCK" and line.stock_kit_lines:
            for sk in line.stock_kit_lines:
                n, cost, sf, bd = stock_fn(
                    db,
                    kit_id=sk.kit_id,
                    use_entire=sk.use_entire,
                    blanks_used=sk.blanks_used,
                    client_id=header.existing_client_id,
                    usage_by_key=sk.usage_by_key,
                )
                usage_cost = 0.0 if exclude_main_stock_cost else cost
                usage_sf = 0.0 if exclude_main_stock_cost else sf
                usages.append((sk.kit_id, n, usage_cost, bd))
                kit_cost_total += usage_cost
                kit_studio_fund += usage_sf
        if kind == "OWN" and line.own_extra_blanks:
            extra_lines = line.own_extra_stock_kit_lines or (
                [
                    StockKitLineInput(
                        kit_id=line.own_extra_stock_kit_id,
                        use_entire=line.own_extra_stock_use_entire,
                        blanks_used=line.own_extra_stock_blanks_used,
                        usage_by_key=line.own_extra_stock_usage_by_key,
                    )
                ]
                if line.own_extra_stock_kit_id
                else []
            )
            for sk in extra_lines:
                if not sk.kit_id:
                    continue
                n, cost, sf, bd = stock_fn(
                    db,
                    kit_id=sk.kit_id,
                    use_entire=sk.use_entire,
                    blanks_used=sk.blanks_used,
                    client_id=header.existing_client_id,
                    usage_by_key=sk.usage_by_key,
                )
                usages.append((sk.kit_id, n, cost, bd))
                kit_cost_total += cost
                kit_studio_fund += sf
        _build_kit_block_from_input(kinp, db)

    addons = max(0.0, float(line.addon_sales_amount or 0.0))
    addons_detail: dict[str, Any] = {}
    ad = (line.addon_sales_description or "").strip()
    if ad:
        addons_detail["description"] = ad
    addons_details_json = json.dumps(addons_detail, ensure_ascii=False) if addons_detail else None

    grams_total = max(0.0, line.kanekalon_grams) + max(0.0, line.kudri_grams)
    mix_cost = 0.0
    mix_bonus_amount = 0.0
    mix_bonus_master_id = line.mix_bonus_master_id
    if line.mix_source and line.mix_source != MixSource.NO_MIX:
        if line.mix_complexity is None:
            raise ValueError("Укажите сложность смешки")
        coef = mix_complexity_rate_for(db, line.mix_complexity)
        mix_cost = grams_total * coef
        if line.mix_source == MixSource.SELF_MIXED:
            mix_bonus_amount = mix_cost
            if mix_bonus_master_id is None:
                mix_bonus_master_id = default_mix_bonus_master_id
    if mix_bonus_master_id:
        _validate_mix_bonus_master(db, mix_bonus_master_id)

    amort_amount = 0.0
    if line.amortization_level is not None:
        amort_amount = float(AMORTIZATION_LEVEL_RUBLES.get(line.amortization_level.value, 0.0))

    cost_total = mat_cost + kit_cost_total + addons + mix_cost + amort_amount
    base_amount_from_client = float(line.amount_from_client or 0)
    amount_from_client = base_amount_from_client
    client_payment_kind = line.client_payment_kind
    if line.own_correction and line.own_corr_use_custom_amount:
        cost_total += compute_correction_extra_costs(
            db,
            corr_trim_qty=int(line.own_corr_trim_qty or 0),
            corr_hourly_hours=float(line.own_corr_hourly_hours or 0),
            corr_hourly_avg=False,
            corr_wash=bool(line.own_corr_wash),
            corr_circle=bool(line.own_corr_circle),
            corr_steam=bool(line.own_corr_steam),
        )
        corr_amount = float(line.own_corr_custom_amount or 0)
        amount_from_client = base_amount_from_client + corr_amount
        if base_amount_from_client <= 0:
            client_payment_kind = line.own_corr_client_payment_kind
        if corr_amount <= 0:
            raise ValueError("Укажите сумму с клиента для коррекции («Своя сумма»).")
    profit_before = amount_from_client - cost_total
    if line.own_correction and line.own_corr_use_custom_amount and profit_before < -0.01:
        raise ValueError("Сумма с клиента меньше себестоимости коррекции.")
    salon_profit = profit_before * salon_pct
    masters_pool = profit_before - salon_profit

    return VisitServiceLineComputed(
        amount_from_client=amount_from_client,
        client_payment_kind=client_payment_kind,
        client_discount_percent=line.client_discount_percent,
        kanekalon_grams=line.kanekalon_grams,
        kudri_grams=line.kudri_grams,
        mix_source=line.mix_source,
        mix_complexity=line.mix_complexity,
        mix_cost_amount=mix_cost,
        mix_bonus_master_id=mix_bonus_master_id,
        mix_bonus_amount=mix_bonus_amount,
        kanekalon_price_per_gram_at_time=k_snap,
        kudri_price_per_gram_at_time=ku_snap,
        materials_cost_total=mat_cost,
        addons_total=addons,
        addons_details_json=addons_details_json,
        amortization_level=line.amortization_level,
        amortization_amount=amort_amount,
        studio_fund_amount=amort_amount + kit_studio_fund,
        cost_total=cost_total,
        profit_before_split=profit_before,
        salon_cut_pct_at_time=salon_pct,
        salon_profit=salon_profit,
        masters_pool=masters_pool,
        kit_paid_separately=bool(line.kit_paid_separately),
        kit_usages=usages,
    )


def recalc_visit_totals(visit: Visit) -> None:
    """Суммы активных строк → денормализованные поля Visit."""
    active = [s for s in (visit.services or []) if not s.is_cancelled]
    if not active:
        for fld in (
            "amount_from_client",
            "kanekalon_grams",
            "kudri_grams",
            "mix_cost_amount",
            "mix_bonus_amount",
            "materials_cost_total",
            "addons_total",
            "amortization_amount",
            "studio_fund_amount",
            "cost_total",
            "profit_before_split",
            "salon_profit",
            "masters_pool",
        ):
            setattr(visit, fld, 0.0)
        visit.mix_source = None
        visit.mix_complexity = None
        visit.mix_bonus_master_id = None
        visit.amortization_level = None
        visit.kanekalon_price_per_gram_at_time = None
        visit.kudri_price_per_gram_at_time = None
        visit.addons_details_json = None
        visit.kit_paid_separately = False
        visit.client_discount_percent = 0
        return

    visit.amount_from_client = sum(float(s.amount_from_client or 0) for s in active)
    visit.kanekalon_grams = sum(float(s.kanekalon_grams or 0) for s in active)
    visit.kudri_grams = sum(float(s.kudri_grams or 0) for s in active)
    visit.mix_cost_amount = sum(float(s.mix_cost_amount or 0) for s in active)
    visit.mix_bonus_amount = sum(float(s.mix_bonus_amount or 0) for s in active)
    visit.materials_cost_total = sum(float(s.materials_cost_total or 0) for s in active)
    visit.addons_total = sum(float(s.addons_total or 0) for s in active)
    visit.amortization_amount = sum(float(s.amortization_amount or 0) for s in active)
    visit.studio_fund_amount = sum(float(s.studio_fund_amount or 0) for s in active)
    visit.cost_total = sum(float(s.cost_total or 0) for s in active)
    visit.profit_before_split = sum(float(s.profit_before_split or 0) for s in active)
    visit.salon_profit = sum(float(s.salon_profit or 0) for s in active)
    visit.masters_pool = sum(float(s.masters_pool or 0) for s in active)
    visit.client_discount_percent = max(int(s.client_discount_percent or 0) for s in active)
    visit.kit_paid_separately = any(bool(s.kit_paid_separately) for s in active)
    first = active[0]
    visit.salon_cut_pct_at_time = float(first.salon_cut_pct_at_time or 0.5)
    visit.kanekalon_price_per_gram_at_time = first.kanekalon_price_per_gram_at_time
    visit.kudri_price_per_gram_at_time = first.kudri_price_per_gram_at_time
    visit.mix_source = first.mix_source
    visit.mix_complexity = first.mix_complexity
    visit.mix_bonus_master_id = first.mix_bonus_master_id
    visit.amortization_level = first.amortization_level


def _resolve_client(db: Session, header: VisitHeaderInput, *, created_by_label: str | None) -> Client:
    if header.client_mode == "draft":
        if not header.draft_name.strip():
            raise ValueError("Укажите имя клиента для черновика.")
        if not client_has_any_contact(
            header.draft_phone,
            header.draft_telegram,
            header.draft_vk,
            header.draft_instagram,
            header.draft_other_contact,
        ):
            raise ValueError("Для черновика нужен хотя бы один контакт (телефон или соцсеть).")
        client = Client(
            name=header.draft_name.strip()[:200],
            phone=strip_or_none(header.draft_phone, 30),
            telegram=strip_or_none(header.draft_telegram, 100),
            vk=strip_or_none(header.draft_vk, 120),
            instagram=strip_or_none(header.draft_instagram, 120),
            other_contact=strip_or_none(header.draft_other_contact, 200),
            comment=None,
            is_confirmed=False,
            created_by_label=created_by_label,
        )
        db.add(client)
        db.flush()
        return client
    if not header.existing_client_id:
        raise ValueError("Найдите и выберите клиента из списка или переключитесь на «Новый черновик».")
    client = db.get(Client, header.existing_client_id)
    if client is None:
        raise ValueError("Клиент не найден.")
    return client


def _validate_lines_masters(
    db: Session,
    inp: MultiServiceVisitInput,
) -> tuple[list[tuple[int, float]] | None, dict[int, list[tuple[int, float]]]]:
    visit_rows: list[tuple[int, float]] | None = None
    per_line: dict[int, list[tuple[int, float]]] = {}
    if inp.header.masters_scope == VisitMastersScope.VISIT:
        visit_rows = _resolve_visit_master_allocations(db, inp.header.visit_master_allocations)
        if inp.header.same_master_shares_all_services:
            for i, line in enumerate(inp.lines):
                per_line[i] = list(visit_rows)
        return visit_rows, per_line
    for i, line in enumerate(inp.lines):
        allocs = line.service_master_allocations
        if inp.header.same_master_shares_all_services and inp.header.visit_master_allocations:
            allocs = inp.header.visit_master_allocations
        per_line[i] = _resolve_visit_master_allocations(db, allocs)
    return None, per_line


def save_visit_with_services(
    db: Session,
    master_id: int,
    inp: MultiServiceVisitInput,
    *,
    created_by_label: str | None = None,
) -> Visit:
    if not inp.lines:
        raise ValueError("Добавьте хотя бы одну услугу.")

    visit_master_rows, line_master_rows = _validate_lines_masters(db, inp)
    client = _resolve_client(db, inp.header, created_by_label=created_by_label)
    performed_dt = datetime.combine(inp.header.performed_date, datetime.min.time())
    ensure_event_date_in_open_payroll_period(db, performed_dt)

    visit = Visit(
        created_by_user_id=master_id,
        performed_date=performed_dt,
        duration_minutes=max(0, inp.header.duration_minutes),
        client_id=client.id,
        client_type=inp.header.client_type,
        price_type=VisitPriceType.CLIENT,
        client_discount_percent=0,
        client_age_group=client.age_group,
        booking_id=inp.header.booking_id,
        masters_scope=inp.header.masters_scope,
        same_master_shares_all_services=inp.header.same_master_shares_all_services,
        kanekalon_grams=0,
        kudri_grams=0,
        mix_cost_amount=0,
        mix_bonus_amount=0,
        materials_cost_total=0,
        amount_from_client=0,
        addons_total=0,
        cost_total=0,
        profit_before_split=0,
        salon_cut_pct_at_time=get_salon_cut_pct(db, master_id),
        salon_profit=0,
        masters_pool=0,
        studio_fund_amount=0,
        amortization_amount=0,
    )
    db.add(visit)
    db.flush()

    if visit_master_rows:
        for mid, pct in visit_master_rows:
            db.add(VisitMaster(visit_id=visit.id, master_id=mid, percent=pct))
        db.flush()

    for idx, line in enumerate(inp.lines):
        if inp.header.client_type != VisitClientType.SELF and effective_amount_from_client(line) <= 0:
            raise ValueError("Укажите сумму, взятую с клиента.")
        computed = compute_visit_service_line(
            db,
            line,
            inp.header,
            default_mix_bonus_master_id=master_id,
        )
        kinp = _line_kit_inlay_adapter(line, inp.header)
        payload = build_payload_from_input(kinp, db)
        service = db.scalar(
            select(Service)
            .options(selectinload(Service.subcategory).selectinload(ServiceSubcategory.category))
            .where(Service.id == line.service_id, Service.is_active.is_(True))
        )
        assert service and service.subcategory and service.subcategory.category

        vs = VisitService(
            visit_id=visit.id,
            service_id=service.id,
            details_json=json.dumps(payload.model_dump(mode="json"), ensure_ascii=False),
            category_name=service.subcategory.category.name,
            subcategory_name=service.subcategory.name,
            service_name=service.name,
            sort_order=line.sort_order if line.sort_order else idx,
            amount_from_client=computed.amount_from_client,
            client_payment_kind=computed.client_payment_kind,
            client_discount_percent=computed.client_discount_percent,
            kanekalon_grams=computed.kanekalon_grams,
            kudri_grams=computed.kudri_grams,
            mix_source=computed.mix_source,
            mix_complexity=computed.mix_complexity,
            mix_cost_amount=computed.mix_cost_amount,
            mix_bonus_master_id=computed.mix_bonus_master_id,
            mix_bonus_amount=computed.mix_bonus_amount,
            kanekalon_price_per_gram_at_time=computed.kanekalon_price_per_gram_at_time,
            kudri_price_per_gram_at_time=computed.kudri_price_per_gram_at_time,
            materials_cost_total=computed.materials_cost_total,
            addons_total=computed.addons_total,
            addons_details_json=computed.addons_details_json,
            amortization_level=computed.amortization_level,
            amortization_amount=computed.amortization_amount,
            studio_fund_amount=computed.studio_fund_amount,
            cost_total=computed.cost_total,
            profit_before_split=computed.profit_before_split,
            salon_cut_pct_at_time=computed.salon_cut_pct_at_time,
            salon_profit=computed.salon_profit,
            masters_pool=computed.masters_pool,
            kit_paid_separately=computed.kit_paid_separately,
            started_at=line.started_at,
            comment=(line.comment or "").strip() or None,
        )
        db.add(vs)
        db.flush()

        if inp.header.masters_scope == VisitMastersScope.PER_SERVICE:
            for mid, pct in line_master_rows.get(idx, []):
                db.add(VisitServiceMaster(visit_service_id=vs.id, master_id=mid, percent=pct))

        for kid, pieces, camount, bd in computed.kit_usages:
            uj = json.dumps(bd, ensure_ascii=False) if bd else None
            db.add(
                VisitKitUsage(
                    visit_id=visit.id,
                    visit_service_id=vs.id,
                    kit_id=kid,
                    pieces_used=pieces,
                    cost_amount=camount,
                    note=None,
                    usage_breakdown_json=uj,
                )
            )

        if payload.thermo is not None:
            persist_new_thermo_template_if_needed(
                db,
                client_id=client.id,
                details=payload.thermo,
                label_suffix=f"Термо {performed_dt.date().isoformat()}",
            )

    visit = db.scalar(
        select(Visit).options(selectinload(Visit.services)).where(Visit.id == visit.id)
    )
    assert visit is not None
    recalc_visit_totals(visit)
    post_visit_accruals(db, visit, visit.created_by_user_id)
    db.commit()
    db.refresh(visit)
    return visit


def kit_inlay_to_multi(inp: KitInlayFormInput, *, booking_id: int | None = None) -> MultiServiceVisitInput:
    line = VisitServiceLineInput(
        service_id=inp.service_id,
        amount_from_client=inp.amount_from_client,
        client_payment_kind=(
            inp.own_corr_client_payment_kind
            if inp.own_correction and inp.own_corr_use_custom_amount
            else inp.client_payment_kind
        ),
        client_discount_percent=inp.client_discount_percent,
        kanekalon_grams=inp.kanekalon_grams,
        kudri_grams=inp.kudri_grams,
        mix_source=inp.mix_source,
        mix_complexity=inp.mix_complexity,
        mix_bonus_master_id=inp.mix_source == MixSource.SELF_MIXED and None or None,
        amortization_level=inp.amortization_level,
        kit_kind=inp.kit_kind,
        stock_kit_lines=list(inp.stock_kit_lines),
        kit_paid_separately=inp.kit_paid_separately,
        own_origin=inp.own_origin,
        own_correction=inp.own_correction,
        own_extra_blanks=inp.own_extra_blanks,
        own_extra_stock_kit_id=inp.own_extra_stock_kit_id,
        own_extra_stock_kit_lines=inp.own_extra_stock_kit_lines,
        own_extra_stock_use_entire=inp.own_extra_stock_use_entire,
        own_extra_stock_blanks_used=inp.own_extra_stock_blanks_used,
        own_extra_stock_usage_by_key=inp.own_extra_stock_usage_by_key,
        own_corr_trim_qty=inp.own_corr_trim_qty,
        own_corr_hourly_hours=inp.own_corr_hourly_hours,
        own_corr_kit_description=inp.own_corr_kit_description,
        own_corr_kit_blanks_count=inp.own_corr_kit_blanks_count,
        own_corr_wash=inp.own_corr_wash,
        own_corr_circle=inp.own_corr_circle,
        own_corr_steam=inp.own_corr_steam,
        own_corr_use_custom_amount=inp.own_corr_use_custom_amount,
        own_corr_custom_amount=inp.own_corr_custom_amount,
        own_corr_client_payment_kind=inp.own_corr_client_payment_kind,
        service_master_allocations=inp.visit_master_allocations,
        questionnaire_raw=inp.questionnaire_raw,
        addon_sales_amount=inp.addon_sales_amount,
        addon_sales_description=inp.addon_sales_description,
        thermo_parsed=inp.thermo_parsed,
    )
    header = VisitHeaderInput(
        client_mode=inp.client_mode,
        existing_client_id=inp.existing_client_id,
        draft_name=inp.draft_name,
        draft_phone=inp.draft_phone,
        draft_telegram=inp.draft_telegram,
        draft_vk=inp.draft_vk,
        draft_instagram=inp.draft_instagram,
        draft_other_contact=inp.draft_other_contact,
        client_type=inp.client_type,
        performed_date=inp.performed_date,
        duration_minutes=inp.duration_minutes,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
        visit_master_allocations=inp.visit_master_allocations,
        booking_id=booking_id,
    )
    return MultiServiceVisitInput(header=header, lines=[line])


def _active_services_summary(visit: Visit) -> str:
    active = [s for s in (visit.services or []) if not s.is_cancelled]
    payload = [
        {
            "id": int(s.id or 0),
            "service_id": int(s.service_id or 0),
            "sort_order": int(s.sort_order or 0),
            "amount_from_client": round(float(s.amount_from_client or 0), 2),
            "comment": s.comment or "",
        }
        for s in sorted(active, key=lambda x: (int(x.sort_order or 0), int(x.id or 0)))
    ]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _visit_masters_summary(visit: Visit) -> str:
    payload = [
        {
            "master_id": int(vm.master_id or 0),
            "percent": round(float(vm.percent or 0), 2),
        }
        for vm in sorted((visit.masters or []), key=lambda x: (int(x.master_id or 0), int(x.id or 0)))
    ]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _line0_present_in_form(form: Any) -> bool:
    """Первая услуга в форме: service_id или скрытый visit_service_id (режим редактирования)."""
    if _line_service_id_from_form(form, 0) > 0:
        return True
    raw = form.get("visit_service_id")
    if raw is None or isinstance(raw, UploadFile):
        return False
    s = raw.decode().strip() if isinstance(raw, (bytes, bytearray)) else str(raw).strip()
    return bool(s.isdigit() and int(s) > 0)


def _discover_line_indices(form: Any) -> list[int]:
    indices: set[int] = set()
    for key in form.keys():
        if not isinstance(key, str):
            continue
        m = _LINE_KEY_RE.match(key)
        if m:
            indices.add(int(m.group(1)))
    result: list[int] = []
    for idx in sorted(indices):
        if idx == 0 and _line_service_id_from_form(form, 0) > 0:
            result.append(0)
        elif idx >= 1 and _line_service_id_from_form(form, idx) > 0:
            result.append(idx)
    if 0 not in result and _line0_present_in_form(form):
        result.insert(0, 0)
    if result:
        return sorted(result)
    if _line_service_id_from_form(form, 0) > 0 or form.get("line_count"):
        return [0]
    return []


def _prefix_g(form: Any, prefix: str, *, allow_unprefixed_fallback: bool = True) -> Callable[[str, str], str]:
    _radio_fields = frozenset({"kit_kind", "mix_source", "mix_complexity", "amortization_level", "own_origin"})

    def g(name: str, default: str = "") -> str:
        full = f"{prefix}{name}" if prefix else name
        v = None
        if name in _radio_fields and hasattr(form, "getlist"):
            vals = [x for x in form.getlist(full) if x is not None and not isinstance(x, UploadFile)]
            if not vals and prefix and allow_unprefixed_fallback:
                vals = [x for x in form.getlist(name) if x is not None and not isinstance(x, UploadFile)]
            if vals:
                v = vals[-1]
        if v is None:
            v = form.get(full)
        if v is None and prefix and allow_unprefixed_fallback:
            v = form.get(name)
        if v is None:
            return default
        if isinstance(v, UploadFile):
            return default
        if isinstance(v, (bytes, bytearray)):
            return v.decode().strip()
        return str(v).strip()

    return g


def _parse_line_from_form(form: Any, idx: int, *, q_prefix: str = "") -> VisitServiceLineInput:
    prefix = f"line_{idx}_"
    g = _prefix_g(form, prefix, allow_unprefixed_fallback=(idx == 0))
    g_int = lambda name, default=0: int(parse_float(g(name, "").strip() or "0", default=default, field_name=name)) if g(name, "").strip() else default
    g_float = lambda name, default=0.0: parse_float(g(name, "").strip() or "0", default=default, field_name=name) if g(name, "").strip() else default
    g_bool = lambda name: parse_bool(g(name, ""))

    kanekalon_grams = g_float("kanekalon_grams", 0)
    kudri_grams = g_float("kudri_grams", 0)
    grams_total = max(0.0, kanekalon_grams) + max(0.0, kudri_grams)
    mix_raw = g("mix_source", "")
    if grams_total <= 0:
        mix = MixSource.NO_MIX
    else:
        mix = MixSource.NO_MIX
        if mix_raw:
            try:
                mix = MixSource(mix_raw)
            except ValueError:
                mix = MixSource.NO_MIX
    comp: MixComplexity | None = None
    if grams_total > 0 and mix != MixSource.NO_MIX:
        comp_raw = (g("mix_complexity", "") or "").strip().upper()
        comp_raw = {"SIMPLE": "STANDARD", "MEDIUM": "KANEK", "HARD": "THERMO"}.get(comp_raw, comp_raw)
        if comp_raw:
            try:
                comp = MixComplexity(comp_raw)
            except ValueError:
                comp = None
    amort_raw = g("amortization_level", "") or "MIN"
    if str(amort_raw).strip().upper() in ("NONE", "NO", "0"):
        amort = None
    else:
        try:
            amort = AmortizationLevel(amort_raw)
        except ValueError:
            amort = AmortizationLevel.MIN

    stock_lines = _parse_stock_kit_lines_from_form(
        _LineFormAdapter(form, prefix),
        g,
        lambda n, d=0: int(parse_float(g(n, "").strip() or "0", field_name=n)) if g(n, "").strip() else d,
        g_bool,
    )
    mix_bonus_raw = g("mix_bonus_master_id", "").strip()
    mix_bonus_id = int(mix_bonus_raw) if mix_bonus_raw.isdigit() and int(mix_bonus_raw) > 0 else None

    q_raw = extract_line_questionnaire_raw_from_form(form, idx)
    if idx == 0:
        legacy_q = extract_questionnaire_raw_from_form(form)
        for k, v in legacy_q.items():
            if k not in q_raw:
                q_raw[k] = v

    started_at: datetime | None = None
    st_raw = g("started_time", "").strip()
    if st_raw:
        try:
            parts = st_raw.split(":")
            h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
            started_at = datetime.combine(date.today(), time(h, m))
        except (ValueError, IndexError):
            started_at = None

    own_extra_stock_kit_lines = _parse_stock_kit_lines_from_form(
        _LineFormAdapter(form, prefix),
        g,
        lambda n, d=0: int(parse_float(g(n, "").strip() or "0", field_name=n)) if g(n, "").strip() else d,
        g_bool,
        lines_json_field="own_extra_stock_kit_lines_json",
        legacy_kit_id_field="own_extra_stock_kit_id",
        legacy_use_entire_field="own_extra_stock_use_entire",
        legacy_blanks_field="own_extra_stock_blanks_used",
        legacy_breakdown_field="own_extra_stock_breakdown_json",
    )
    own_extra_kit_id = (
        own_extra_stock_kit_lines[0].kit_id
        if own_extra_stock_kit_lines
        else (int(g("own_extra_stock_kit_id", "0") or "0") or None)
    )

    vs_id_raw = g("visit_service_id", "").strip()
    visit_service_id: int | None = None
    if vs_id_raw.isdigit() and int(vs_id_raw) > 0:
        visit_service_id = int(vs_id_raw)

    own_corr_use_custom = g_bool("own_corr_use_custom_amount")
    own_corr_custom = max(0.0, g_float("own_corr_custom_amount", 0))
    if g_bool("own_correction") and not own_corr_use_custom and own_corr_custom > 0:
        own_corr_use_custom = True
    own_corr_pk = parse_client_payment_kind(g("own_corr_client_payment_kind", ""))
    line_amount = g_float("amount_from_client", 0)
    line_pk = parse_client_payment_kind(g("client_payment_kind", ""))
    if own_corr_use_custom and line_amount <= 0:
        line_pk = own_corr_pk

    return VisitServiceLineInput(
        service_id=int(parse_float(g("service_id", "0") or "0", field_name="service_id")),
        amount_from_client=line_amount,
        client_payment_kind=line_pk,
        client_discount_percent=_parse_visit_client_discount_percent(g),
        kanekalon_grams=kanekalon_grams,
        kudri_grams=kudri_grams,
        mix_source=mix,
        mix_complexity=comp,
        mix_bonus_master_id=mix_bonus_id,
        amortization_level=amort,
        kit_kind=g("kit_kind", "STOCK").upper(),
        stock_kit_lines=stock_lines,
        kit_paid_separately=g_bool("kit_paid_separately"),
        own_origin=g("own_origin") or None,
        own_correction=g_bool("own_correction"),
        own_extra_blanks=g_bool("own_extra_blanks"),
        own_extra_stock_kit_id=own_extra_kit_id,
        own_extra_stock_kit_lines=own_extra_stock_kit_lines,
        own_extra_stock_use_entire=(
            own_extra_stock_kit_lines[0].use_entire
            if own_extra_stock_kit_lines
            else g_bool("own_extra_stock_use_entire")
        ),
        own_extra_stock_blanks_used=(
            own_extra_stock_kit_lines[0].blanks_used
            if own_extra_stock_kit_lines
            else int(g("own_extra_stock_blanks_used", "0") or "0")
        ),
        own_extra_stock_usage_by_key=(
            own_extra_stock_kit_lines[0].usage_by_key
            if own_extra_stock_kit_lines
            else _parse_stock_breakdown_json(g, "own_extra_stock_breakdown_json")
        ),
        own_corr_trim_qty=int(g("own_corr_trim_qty", "0") or "0"),
        own_corr_hourly_hours=max(0.0, g_float("own_corr_hourly_hours", 0)),
        own_corr_kit_description=g("own_corr_kit_description", ""),
        own_corr_kit_blanks_count=_parse_optional_nonneg_int(g, "own_corr_kit_blanks_count"),
        own_corr_wash=g_bool("own_corr_wash"),
        own_corr_circle=g_bool("own_corr_circle"),
        own_corr_steam=g_bool("own_corr_steam"),
        own_corr_use_custom_amount=own_corr_use_custom,
        own_corr_custom_amount=own_corr_custom,
        own_corr_client_payment_kind=own_corr_pk,
        service_master_allocations=_parse_service_master_allocations_from_form(form, idx),
        questionnaire_raw=q_raw,
        addon_sales_amount=max(0.0, g_float("addon_sales_amount", 0)),
        addon_sales_description=g("addon_sales_description", ""),
        thermo_parsed=parse_thermo_from_form(_LineFormAdapter(form, prefix)),
        started_at=started_at,
        comment=g("comment", "") or None,
        sort_order=idx,
        visit_service_id=visit_service_id,
    )


class _LineFormAdapter:
    """Прокси FormData с префиксом line_N_ для парсеров kit_inlay."""

    def __init__(self, form: Any, prefix: str) -> None:
        self._form = form
        self._prefix = prefix

    def get(self, name: str, default: Any = None) -> Any:
        v = self._form.get(self._prefix + name)
        if v is None:
            v = self._form.get(name)
        return v if v is not None else default

    def getlist(self, name: str) -> list[Any]:
        vs = list(self._form.getlist(self._prefix + name))
        if not vs:
            vs = list(self._form.getlist(name))
        return vs

    def keys(self) -> Any:
        return self._form.keys()


def _parse_service_master_allocations_from_form(form: Any, line_idx: int) -> list[tuple[int, int]]:
    prefix = f"line_{line_idx}_"
    active: list[int] = []
    for x in form.getlist(f"{prefix}service_master_on"):
        if isinstance(x, UploadFile):
            continue
        try:
            s = x.decode().strip() if isinstance(x, (bytes, bytearray)) else str(x).strip()
            i = int(s)
        except (ValueError, AttributeError):
            continue
        if i > 0 and i not in active:
            active.append(i)
    if not active:
        return []
    rows: list[tuple[int, int]] = []
    if len(active) == 1:
        mid = active[0]
        raw = form.get(f"{prefix}service_master_pct_{mid}")
        s = ""
        if raw and not isinstance(raw, UploadFile):
            s = raw.decode().strip() if isinstance(raw, (bytes, bytearray)) else str(raw).strip()
        p = 100 if not s else int(s)
        return [(mid, p)]
    for mid in active:
        raw = form.get(f"{prefix}service_master_pct_{mid}")
        if not raw or isinstance(raw, UploadFile):
            raise ValueError("Для каждого отмеченного мастера услуги укажите целый процент.")
        s = raw.decode().strip() if isinstance(raw, (bytes, bytearray)) else str(raw).strip()
        rows.append((mid, int(s)))
    return rows


def parse_multi_service_visit_form(
    form: Any,
    *,
    single_master_default_id: int | None = None,
    booking_id: int | None = None,
) -> MultiServiceVisitInput:
    g = _prefix_g(form, "")

    def g_int(name: str, default: int = 0) -> int:
        raw = g(name, "").strip()
        if not raw:
            return default
        return int(parse_float(raw, field_name=name))

    def g_bool(name: str) -> bool:
        return parse_bool(g(name, ""))

    indices = _discover_line_indices(form)
    if not indices:
        raise ValueError("Добавьте хотя бы одну услугу.")

    scope_raw = (g("masters_scope", "VISIT") or "VISIT").strip().upper()
    try:
        masters_scope = VisitMastersScope(scope_raw)
    except ValueError:
        masters_scope = VisitMastersScope.VISIT

    ct = VisitClientType.SELF if g_bool("client_is_self") else VisitClientType.RETURNING
    pd_raw = g("performed_date", "")
    try:
        performed_date = parse_date_iso(pd_raw, field_name="performed_date") if pd_raw else date.today()
    except ValueError:
        performed_date = date.today()

    mode_raw = (g("client_mode", "existing") or "existing").lower()
    client_mode = "draft" if mode_raw == "draft" else "existing"
    eid = g_int("existing_client_id", 0)
    existing_client_id = eid if eid > 0 else None

    if masters_scope == VisitMastersScope.PER_SERVICE:
        visit_master_allocations: list[tuple[int, int]] = []
    elif single_master_default_id is not None and not g_bool("visit_use_multi_masters"):
        visit_master_allocations = [(single_master_default_id, 100)]
    else:
        # Без single_master_default (админ без роли мастера / редактирование) —
        # только явный выбор из списка.
        visit_master_allocations = _parse_visit_master_allocations_from_form(form)

    header = VisitHeaderInput(
        client_mode=client_mode,
        existing_client_id=existing_client_id,
        draft_name=g("draft_client_name"),
        draft_phone=g("draft_phone"),
        draft_telegram=g("draft_telegram"),
        draft_vk=g("draft_vk"),
        draft_instagram=g("draft_instagram"),
        draft_other_contact=g("draft_other_contact"),
        client_type=ct,
        performed_date=performed_date,
        duration_minutes=g_int("duration_h", 0) * 60 + g_int("duration_m", 0),
        masters_scope=masters_scope,
        same_master_shares_all_services=g_bool("same_master_shares_all_services"),
        visit_master_allocations=visit_master_allocations,
        booking_id=booking_id,
    )

    lines = [_parse_line_from_form(form, i) for i in indices]
    return MultiServiceVisitInput(header=header, lines=lines)


def _master_rows_for_line(
    visit: Visit,
    idx: int,
    line: VisitServiceLineInput,
    line_master_rows: dict[int, list[tuple[int, float]]],
) -> list[tuple[int, float]]:
    if visit.masters_scope == VisitMastersScope.PER_SERVICE:
        return line_master_rows.get(idx, [])
    return []


def _visit_service_financial_signature(
    db: Session,
    vs: VisitService,
    visit: Visit,
) -> tuple:
    if visit.masters_scope == VisitMastersScope.PER_SERVICE:
        masters = tuple(
            (int(m.master_id), int(m.percent or 0))
            for m in db.scalars(
                select(VisitServiceMaster)
                .where(VisitServiceMaster.visit_service_id == vs.id)
                .order_by(VisitServiceMaster.master_id.asc())
            ).all()
        )
    else:
        masters = tuple(
            (int(m.master_id), int(m.percent or 0))
            for m in db.scalars(
                select(VisitMaster).where(VisitMaster.visit_id == visit.id).order_by(VisitMaster.master_id.asc())
            ).all()
        )
    return (
        int(vs.service_id or 0),
        round(float(vs.amount_from_client or 0), 2),
        round(float(vs.salon_profit or 0), 2),
        round(float(vs.studio_fund_amount or 0), 2),
        round(float(vs.masters_pool or 0), 2),
        int(vs.mix_bonus_master_id or 0),
        round(float(vs.mix_bonus_amount or 0), 2),
        masters,
    )


def _apply_computed_to_visit_service(vs: VisitService, computed: VisitServiceLineComputed, line: VisitServiceLineInput) -> None:
    vs.amount_from_client = computed.amount_from_client
    vs.client_payment_kind = computed.client_payment_kind
    vs.client_discount_percent = computed.client_discount_percent
    vs.kanekalon_grams = computed.kanekalon_grams
    vs.kudri_grams = computed.kudri_grams
    vs.mix_source = computed.mix_source
    vs.mix_complexity = computed.mix_complexity
    vs.mix_cost_amount = computed.mix_cost_amount
    vs.mix_bonus_master_id = computed.mix_bonus_master_id
    vs.mix_bonus_amount = computed.mix_bonus_amount
    vs.kanekalon_price_per_gram_at_time = computed.kanekalon_price_per_gram_at_time
    vs.kudri_price_per_gram_at_time = computed.kudri_price_per_gram_at_time
    vs.materials_cost_total = computed.materials_cost_total
    vs.addons_total = computed.addons_total
    vs.addons_details_json = computed.addons_details_json
    vs.amortization_level = computed.amortization_level
    vs.amortization_amount = computed.amortization_amount
    vs.studio_fund_amount = computed.studio_fund_amount
    vs.cost_total = computed.cost_total
    vs.profit_before_split = computed.profit_before_split
    vs.salon_cut_pct_at_time = computed.salon_cut_pct_at_time
    vs.salon_profit = computed.salon_profit
    vs.masters_pool = computed.masters_pool
    vs.kit_paid_separately = computed.kit_paid_separately
    vs.started_at = line.started_at
    vs.comment = (line.comment or "").strip() or None


def _persist_kit_usages_for_service(
    db: Session,
    visit: Visit,
    vs: VisitService,
    computed: VisitServiceLineComputed,
) -> None:
    for kid, pieces, camount, bd in computed.kit_usages:
        uj = json.dumps(bd, ensure_ascii=False) if bd else None
        db.add(
            VisitKitUsage(
                visit_id=visit.id,
                visit_service_id=vs.id,
                kit_id=kid,
                pieces_used=pieces,
                cost_amount=camount,
                note=None,
                usage_breakdown_json=uj,
            )
        )


def _persist_service_masters(
    db: Session,
    vs: VisitService,
    master_rows: list[tuple[int, float]],
) -> None:
    for mid, pct in master_rows:
        db.add(VisitServiceMaster(visit_service_id=vs.id, master_id=mid, percent=pct))


def _insert_visit_service_from_line(
    db: Session,
    visit: Visit,
    client: Client,
    inp: MultiServiceVisitInput,
    idx: int,
    line: VisitServiceLineInput,
    line_master_rows: dict[int, list[tuple[int, float]]],
    *,
    editor_user_id: int,
    performed_dt: datetime,
) -> VisitService:
    if inp.header.client_type != VisitClientType.SELF and effective_amount_from_client(line) <= 0:
        raise ValueError("Укажите сумму, взятую с клиента.")
    computed = compute_visit_service_line(
        db,
        line,
        inp.header,
        default_mix_bonus_master_id=editor_user_id,
    )
    kinp = _line_kit_inlay_adapter(line, inp.header)
    payload = build_payload_from_input(kinp, db)
    service = db.scalar(
        select(Service)
        .options(selectinload(Service.subcategory).selectinload(ServiceSubcategory.category))
        .where(Service.id == line.service_id, Service.is_active.is_(True))
    )
    assert service and service.subcategory and service.subcategory.category

    vs = VisitService(
        visit_id=visit.id,
        service_id=service.id,
        details_json=json.dumps(payload.model_dump(mode="json"), ensure_ascii=False),
        category_name=service.subcategory.category.name,
        subcategory_name=service.subcategory.name,
        service_name=service.name,
        sort_order=line.sort_order if line.sort_order else idx,
        amount_from_client=0,
        client_discount_percent=0,
        kanekalon_grams=0,
        kudri_grams=0,
        mix_cost_amount=0,
        mix_bonus_amount=0,
        materials_cost_total=0,
        addons_total=0,
        cost_total=0,
        profit_before_split=0,
        salon_cut_pct_at_time=computed.salon_cut_pct_at_time,
        salon_profit=0,
        masters_pool=0,
        kit_paid_separately=False,
    )
    db.add(vs)
    db.flush()
    _apply_computed_to_visit_service(vs, computed, line)
    vs.details_json = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False)

    master_rows = _master_rows_for_line(visit, idx, line, line_master_rows)
    if master_rows:
        _persist_service_masters(db, vs, master_rows)
    _persist_kit_usages_for_service(db, visit, vs, computed)

    if payload.thermo is not None:
        persist_new_thermo_template_if_needed(
            db,
            client_id=client.id,
            details=payload.thermo,
            label_suffix=f"Термо {performed_dt.date().isoformat()}",
        )
    return vs


def _cancel_visit_service_line(
    db: Session,
    visit: Visit,
    vs: VisitService,
    *,
    editor_user_id: int,
) -> None:
    ok, err = visit_service_revert_stock(db, vs.id)
    if not ok:
        raise ValueError(err or "Не удалось откатить склад по услуге.")
    storno_source_accruals(db, PayrollFundSourceKind.VISIT_SERVICE, vs.id, editor_user_id)
    vs.is_cancelled = True
    vs.cancelled_at = utcnow_naive()
    vs.cancelled_by_user_id = editor_user_id


def update_visit_with_services(
    db: Session,
    visit_id: int,
    editor_user_id: int,
    inp: MultiServiceVisitInput,
) -> Visit:
    if not inp.lines:
        raise ValueError("Добавьте хотя бы одну услугу.")

    visit = db.scalar(
        select(Visit)
        .options(
            selectinload(Visit.services),
            selectinload(Visit.kit_usages),
            selectinload(Visit.masters),
        )
        .where(Visit.id == visit_id)
    )
    if not visit:
        raise ValueError("Визит не найден.")
    if visit.is_cancelled:
        raise ValueError("Визит отменён — редактирование невозможно.")

    before = {
        "client_id": visit.client_id,
        "client_type": visit.client_type,
        "performed_date": visit.performed_date,
        "duration_minutes": visit.duration_minutes,
        "masters_scope": visit.masters_scope,
        "same_master_shares_all_services": visit.same_master_shares_all_services,
        "amount_from_client": float(visit.amount_from_client or 0),
        "cost_total": float(visit.cost_total or 0),
        "profit_before_split": float(visit.profit_before_split or 0),
        "salon_profit": float(visit.salon_profit or 0),
        "masters_pool": float(visit.masters_pool or 0),
        "services_summary": _active_services_summary(visit),
        "visit_masters_summary": _visit_masters_summary(visit),
    }

    visit_master_rows, line_master_rows = _validate_lines_masters(db, inp)
    client = _resolve_client(db, inp.header, created_by_label=None)
    performed_dt = datetime.combine(inp.header.performed_date, datetime.min.time())
    if visit.performed_date != performed_dt:
        ensure_event_date_in_open_payroll_period(db, performed_dt)

    active_before = {
        vs.id: vs
        for vs in (visit.services or [])
        if not vs.is_cancelled
    }
    sig_before = {vs_id: _visit_service_financial_signature(db, vs, visit) for vs_id, vs in active_before.items()}

    visit.performed_date = performed_dt
    visit.duration_minutes = max(0, inp.header.duration_minutes)
    visit.client_id = client.id
    visit.client_type = inp.header.client_type
    visit.client_age_group = client.age_group
    visit.masters_scope = inp.header.masters_scope
    visit.same_master_shares_all_services = inp.header.same_master_shares_all_services
    if inp.header.booking_id is not None:
        visit.booking_id = inp.header.booking_id
    visit.updated_at = utcnow_naive()
    visit.updated_by_user_id = editor_user_id

    for vm in list(visit.masters or []):
        db.delete(vm)
    db.flush()
    if visit_master_rows:
        for mid, pct in visit_master_rows:
            db.add(VisitMaster(visit_id=visit.id, master_id=mid, percent=pct))
        db.flush()

    kept_ids: set[int] = set()
    for idx, line in enumerate(inp.lines):
        if line.visit_service_id and line.visit_service_id in active_before:
            vs = active_before[line.visit_service_id]
            kept_ids.add(vs.id)
            ok, err = visit_service_revert_stock(db, vs.id)
            if not ok:
                raise ValueError(err or "Не удалось откатить склад.")
            for vsm in db.scalars(
                select(VisitServiceMaster).where(VisitServiceMaster.visit_service_id == vs.id)
            ).all():
                db.delete(vsm)
            db.flush()

            computed = compute_visit_service_line(
                db,
                line,
                inp.header,
                default_mix_bonus_master_id=editor_user_id,
            )
            kinp = _line_kit_inlay_adapter(line, inp.header)
            payload = build_payload_from_input(kinp, db)
            service = db.scalar(
                select(Service)
                .options(selectinload(Service.subcategory).selectinload(ServiceSubcategory.category))
                .where(Service.id == line.service_id, Service.is_active.is_(True))
            )
            if not service or not service.subcategory or not service.subcategory.category:
                raise ValueError("Услуга не найдена")
            vs.service_id = service.id
            vs.category_name = service.subcategory.category.name
            vs.subcategory_name = service.subcategory.name
            vs.service_name = service.name
            vs.sort_order = line.sort_order if line.sort_order else idx
            vs.details_json = json.dumps(payload.model_dump(mode="json"), ensure_ascii=False)
            _apply_computed_to_visit_service(vs, computed, line)

            master_rows = _master_rows_for_line(visit, idx, line, line_master_rows)
            if master_rows:
                _persist_service_masters(db, vs, master_rows)
            _persist_kit_usages_for_service(db, visit, vs, computed)

            if payload.thermo is not None:
                persist_new_thermo_template_if_needed(
                    db,
                    client_id=client.id,
                    details=payload.thermo,
                    label_suffix=f"Термо {performed_dt.date().isoformat()}",
                )
        else:
            vs = _insert_visit_service_from_line(
                db,
                visit,
                client,
                inp,
                idx,
                line,
                line_master_rows,
                editor_user_id=editor_user_id,
                performed_dt=performed_dt,
            )
            kept_ids.add(vs.id)
            db.flush()
            post_visit_service_accruals(db, vs, visit, editor_user_id)

    for vs_id, vs in active_before.items():
        if vs_id not in kept_ids:
            _cancel_visit_service_line(db, visit, vs, editor_user_id=editor_user_id)

    db.flush()
    visit = db.scalar(
        select(Visit).options(selectinload(Visit.services), selectinload(Visit.masters)).where(Visit.id == visit.id)
    )
    assert visit is not None
    recalc_visit_totals(visit)

    after = {
        "client_id": visit.client_id,
        "client_type": visit.client_type,
        "performed_date": visit.performed_date,
        "duration_minutes": visit.duration_minutes,
        "masters_scope": visit.masters_scope,
        "same_master_shares_all_services": visit.same_master_shares_all_services,
        "amount_from_client": float(visit.amount_from_client or 0),
        "cost_total": float(visit.cost_total or 0),
        "profit_before_split": float(visit.profit_before_split or 0),
        "salon_profit": float(visit.salon_profit or 0),
        "masters_pool": float(visit.masters_pool or 0),
        "services_summary": _active_services_summary(visit),
        "visit_masters_summary": _visit_masters_summary(visit),
    }
    write_audit_rows(
        db,
        log_model=VisitAuditLog,
        entity_field="visit_id",
        entity_id=visit.id,
        changed_by_user_id=editor_user_id,
        changes=diff_fields(
            type("VisitBefore", (), before)(),
            type("VisitAfter", (), after)(),
            tuple(before.keys()),
        ),
    )

    for vs in (visit.services or []):
        if vs.is_cancelled:
            continue
        new_sig = _visit_service_financial_signature(db, vs, visit)
        old_sig = sig_before.get(vs.id)
        if old_sig is not None and new_sig != old_sig:
            replace_visit_service_accruals(db, vs, visit, editor_user_id)
        elif old_sig is None and vs.id not in sig_before:
            pass  # already posted for new lines

    db.commit()
    db.refresh(visit)
    return visit


def read_visit_master_form_state_multi(form: Any) -> tuple[dict[str, str], list[int], dict[int, str]]:
    fp: dict[str, str] = {}
    for key in form.keys():
        if key == "visit_master_on" or str(key).startswith("visit_master_pct_"):
            continue
        if str(key).startswith("q_"):
            continue
        if _LINE_KEY_RE.match(str(key)):
            continue
        last: str | None = None
        for v in form.getlist(key):
            if isinstance(v, UploadFile):
                continue
            last = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
        if last is not None:
            fp[key] = last
    vm_on_ids, vm_pct_str = read_visit_master_form_state(form)
    return fp, vm_on_ids, vm_pct_str
