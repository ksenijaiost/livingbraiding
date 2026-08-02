"""
Форма визита мастера: разбор POST, сохранение визита, расчёт материалов/смешки/амортизации.

Блок комплекта (склад, свой) только для подкатегории «Вплетение комплекта»;
остальные услуги сохраняют `details_json` с `kit: null` и ответами анкеты.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

from sqlalchemy import exists, func, or_, select
from starlette.datastructures import UploadFile
from sqlalchemy.orm import Session, selectinload

from app.client_payment import parse_client_payment_kind
from app.db.models import (
    Client,
    ClientPaymentKind,
    Kit,
    KitReserve,
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
from app.mix_rates import mix_complexity_rate_for
from app.forms_parse import parse_bool, parse_date_iso, parse_float
from app.setting_keys import KIT_MAX_RESERVES_PER_KIT, SALON_CUT_PCT

# Фиксированные суммы амортизации по уровню (себестоимость визита); подписи на форме — из этого же словаря.
AMORTIZATION_LEVEL_RUBLES: dict[str, float] = {
    AmortizationLevel.MIN.value: 100.0,
    AmortizationLevel.MID.value: 200.0,
    AmortizationLevel.MAX.value: 500.0,
}


def get_kit_max_reserves_per_kit(db: Session) -> int:
    row = db.get(Setting, KIT_MAX_RESERVES_PER_KIT)
    raw = (row.value if row else "3").strip()
    try:
        n = int(raw)
        return max(1, min(20, n))
    except ValueError:
        return 3


def kit_reserve_slots_used(db: Session, kit_id: int) -> int:
    return int(
        db.scalar(select(func.count()).select_from(KitReserve).where(KitReserve.kit_id == kit_id)) or 0
    )


def _kit_reserve_target_short(reserve: KitReserve) -> str:
    parts: list[str] = []
    if reserve.reserved_for_client:
        n = (reserve.reserved_for_client.name or "").strip() or "—"
        parts.append(f"клиент «{n}»")
    if reserve.reserved_for_user:
        dn = (reserve.reserved_for_user.display_name or "").strip() or "—"
        parts.append(f"сотрудник «{dn}»")
    if not parts:
        return "цель не указана"
    return " и ".join(parts)


from app.questionnaire.answer_validate import (
    extract_questionnaire_raw_from_form,
    validate_and_coerce_answers,
)
from app.questionnaire.runtime_merge import (
    MergedQuestionnaireFieldSpec,
    load_merged_questionnaire_specs,
    merged_questionnaire_client_json,
)
from app.questionnaire.schemas import (
    KitBlock,
    KitFromStock,
    KitOwn,
    KitOwnCorrectionDetails,
    KitOwnExtra,
    VisitServiceDetailsPayload,
    parse_visit_service_details,
)
from app.thermo_visit import (
    ThermoFormParsed,
    build_thermo_visit_details,
    parse_thermo_from_form,
    persist_new_thermo_template_if_needed,
    service_requires_thermo_flow,
)
from app.kit_blank_stock_core import (
    apply_discount_capped,
    blank_stock_qty_map,
    build_usage_breakdown_keyed,
    keyed_client_price_selected,
    keyed_cost_selected,
    kit_inventory_is_keyed,
    load_catalog_kit_maps,
    max_take_by_key_for_client,
    parse_composition_totals,
    release_client_kit_reserves_into_free_pool,
    require_composition_stock_rows_or_scalar_ok,
    decrement_blank_stock_keys,
    sync_kit_pieces_available_from_blank_lines,
)
from app.payroll_fund import post_visit_accruals
from app.user_roles import user_has_role
from app.visit_edit_policy import ensure_event_date_in_open_payroll_period


def service_requires_kit_block(service: Service) -> bool:
    if service.kit_section_override is not None:
        return bool(service.kit_section_override)
    sub = service.subcategory
    return bool(sub and sub.show_kit_section)


def service_requires_tail_block(service: Service) -> bool:
    if getattr(service, "tail_section_override", None) is not None:
        return bool(service.tail_section_override)
    sub = service.subcategory
    return bool(sub and getattr(sub, "show_tail_section", False))


def effective_requires_kit_block(
    service: Service,
    answers: dict[str, Any] | None = None,
    specs: list | None = None,
) -> bool:
    """Комплект нужен по флагу услуги/подкатегории или по отмеченной галочке анкеты."""
    if service_requires_kit_block(service):
        return True
    if not answers or not specs:
        return False
    from app.questionnaire.reveal import REVEAL_BLOCK_KIT, answers_reveal_blocks

    return REVEAL_BLOCK_KIT in answers_reveal_blocks(answers, specs)


def effective_requires_tail_block(
    service: Service,
    answers: dict[str, Any] | None = None,
    specs: list | None = None,
) -> bool:
    if service_requires_tail_block(service):
        return True
    if not answers or not specs:
        return False
    from app.questionnaire.reveal import REVEAL_BLOCK_TAIL, answers_reveal_blocks

    return REVEAL_BLOCK_TAIL in answers_reveal_blocks(answers, specs)


def effective_requires_thermo_block(
    service: Service,
    answers: dict[str, Any] | None = None,
    specs: list | None = None,
) -> bool:
    from app.thermo_visit import service_requires_thermo_flow

    if service_requires_thermo_flow(service):
        return True
    if not answers or not specs:
        return False
    from app.questionnaire.reveal import REVEAL_BLOCK_THERMO, answers_reveal_blocks

    return REVEAL_BLOCK_THERMO in answers_reveal_blocks(answers, specs)


def get_salon_cut_pct(db: Session, master_id: int | None = None) -> float:
    if master_id is not None and int(master_id) > 0:
        u = db.get(User, int(master_id))
        if u is not None and u.salon_cut_pct_override is not None:
            try:
                v = float(u.salon_cut_pct_override)
            except Exception:
                v = -1.0
            if 0.0 <= v <= 1.0:
                return v
    row = db.get(Setting, SALON_CUT_PCT)
    if not row:
        return 0.5
    try:
        return parse_float(row.value, default=0.5, field_name=SALON_CUT_PCT)
    except ValueError:
        return 0.5


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
    client_id: int | None = None,
    usage_by_key: dict[str, int] | None = None,
) -> Kit:
    """Проверка без списания (для сборки JSON)."""
    kit = db.scalar(
        select(Kit)
        .where(Kit.id == int(kit_id))
        .options(selectinload(Kit.reserves), selectinload(Kit.blank_stock_lines))
    )
    if not kit or kit.is_archived or not kit.is_active:
        raise ValueError("Комплект не найден или недоступен")
    require_composition_stock_rows_or_scalar_ok(db, kit)

    if kit_inventory_is_keyed(db, int(kit_id)):
        stock_map = blank_stock_qty_map(db, int(kit_id))
        if not stock_map or sum(stock_map.values()) <= 0:
            raise ValueError("Нет заготовок на складе по этому комплекту (остатки по видам).")
        max_by_key = max_take_by_key_for_client(db, kit=kit, client_id=client_id, stock_map=stock_map)
        if sum(max_by_key.values()) <= 0:
            raise ValueError("Нет заготовок на складе по этому комплекту")
        try:
            build_usage_breakdown_keyed(
                use_entire=use_entire,
                blanks_used=blanks_used,
                usage_by_key=usage_by_key,
                max_by_key=max_by_key,
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        return kit

    avail = int(kit.pieces_available or 0)
    cid = int(client_id or 0)
    reserved_for_client = 0
    if cid > 0:
        total = db.scalar(
            select(func.coalesce(func.sum(KitReserve.pieces_reserved), 0)).where(
                KitReserve.kit_id == int(kit_id),
                KitReserve.reserved_for_client_id == cid,
            )
        )
        reserved_for_client = int(total or 0)
    max_for_client = int(avail or 0) + int(reserved_for_client or 0)
    if max_for_client <= 0:
        raise ValueError("Нет заготовок на складе по этому комплекту")
    n = max_for_client if use_entire else blanks_used
    if n <= 0:
        raise ValueError("Укажите количество заготовок или «весь комплект»")
    if n > max_for_client:
        if reserved_for_client > 0:
            raise ValueError(
                f"Нельзя списать больше, чем доступно (в наличии {int(avail or 0)} + резерв клиента {reserved_for_client})"
            )
        raise ValueError(f"Нельзя списать больше, чем в наличии ({int(avail or 0)})")
    return kit


def estimate_stock_kit_usage(
    db: Session,
    *,
    kit_id: int,
    use_entire: bool,
    blanks_used: int,
    client_id: int | None = None,
    usage_by_key: dict[str, int] | None = None,
) -> tuple[int, float, float, dict[str, int]]:
    """Расчёт стоимости комплекта без списания со склада."""
    return _apply_stock_kit_usage(
        db,
        kit_id=kit_id,
        use_entire=use_entire,
        blanks_used=blanks_used,
        client_id=client_id,
        usage_by_key=usage_by_key,
        mutate_stock=False,
    )


def _apply_stock_kit_usage(
    db: Session,
    *,
    kit_id: int,
    use_entire: bool,
    blanks_used: int,
    client_id: int | None = None,
    usage_by_key: dict[str, int] | None = None,
    mutate_stock: bool = True,
) -> tuple[int, float, float, dict[str, int]]:
    """Списание: (pieces_used, cost_amount_for_visit, studio_fund_amount, usage_by_key_out).

    cost_amount_for_visit — доля (цена − скидка) по списанным заготовкам (в расход визита/заказа).
    studio_fund — остаток после вычета себестоимости (себестоимость уже включает ЗП авторов).
    """
    kit = _validate_stock_selection(
        db,
        kit_id=kit_id,
        use_entire=use_entire,
        blanks_used=blanks_used,
        client_id=client_id,
        usage_by_key=usage_by_key,
    )
    raw_price = kit.stock_price_total
    if raw_price is None or float(raw_price) <= 0:
        raise ValueError(
            "У комплекта не задана цена продажи. Укажите цену в карточке комплекта (администратор), затем снова выберите комплект."
        )
    price = float(raw_price)

    if kit_inventory_is_keyed(db, int(kit.id)):
        price_map, meta_by_key, _labels = load_catalog_kit_maps(db)
        comp = parse_composition_totals(kit)
        if mutate_stock:
            release_client_kit_reserves_into_free_pool(db, kit=kit, client_id=client_id)
            db.flush()
        stock_map = blank_stock_qty_map(db, int(kit.id))
        max_by_key = max_take_by_key_for_client(db, kit=kit, client_id=client_id, stock_map=stock_map)
        bd = build_usage_breakdown_keyed(
            use_entire=use_entire,
            blanks_used=blanks_used,
            usage_by_key=usage_by_key,
            max_by_key=max_by_key,
        )
        ntot = sum(int(v) for v in bd.values())
        if ntot <= 0:
            raise ValueError("Укажите количество заготовок или «весь комплект»")
        if mutate_stock:
            decrement_blank_stock_keys(db, int(kit.id), bd)
            sync_kit_pieces_available_from_blank_lines(db, kit)
        from app.kit_composition_lines import composition_has_v2_lines, keyed_client_price_selected_v2

        if composition_has_v2_lines(kit.composition_json):
            selected_price = keyed_client_price_selected_v2(
                db,
                kit.composition_json,
                bd,
                price_map=price_map,
                meta_by_key=meta_by_key,
            )
        else:
            selected_price = keyed_client_price_selected(bd, price_map=price_map, meta_by_key=meta_by_key)
        selected_cost = keyed_cost_selected(bd, comp=comp, kit_cost_total=max(0.0, float(kit.cost_total or 0.0)))
        _disc, net = apply_discount_capped(
            selected_price, discount_percent=int(kit.discount_percent or 0), cost_floor=selected_cost
        )
        studio_fund = max(0.0, net - selected_cost)
        if mutate_stock and kit.pieces_available <= 0:
            kit.is_in_stock = False
        return int(ntot), float(net), float(studio_fund), bd

    total_pieces = int(kit.pieces_total) if kit.pieces_total else 1
    if total_pieces <= 0:
        total_pieces = 1
    avail = int(kit.pieces_available or 0)
    cid = int(client_id or 0)
    reserved_for_client = 0
    if cid > 0:
        total = db.scalar(
            select(func.coalesce(func.sum(KitReserve.pieces_reserved), 0)).where(
                KitReserve.kit_id == int(kit.id),
                KitReserve.reserved_for_client_id == cid,
            )
        )
        reserved_for_client = int(total or 0)
        if mutate_stock and reserved_for_client > 0:
            rows = list(
                db.scalars(
                    select(KitReserve)
                    .where(
                        KitReserve.kit_id == int(kit.id),
                        KitReserve.reserved_for_client_id == cid,
                    )
                    .order_by(KitReserve.id.asc())
                ).all()
            )
            for r in rows:
                db.delete(r)
            kit.pieces_available = int(kit.pieces_available or 0) + int(reserved_for_client)
            avail = int(kit.pieces_available or 0)
            reserved_for_client = 0

    max_for_client = int(avail or 0) + int(reserved_for_client or 0)
    n = max_for_client if use_entire else int(blanks_used or 0)
    kit_cost_full = max(0.0, float(kit.cost_total or 0.0))
    max_disc_margin = max(0.0, price - kit_cost_full)
    pct = max(0, min(100, int(kit.discount_percent or 0)))
    discount_full = price * (pct / 100.0)
    discount_full = min(discount_full, max_disc_margin, price)
    net_full = max(0.0, price - discount_full)
    k = float(n) / float(total_pieces)
    cost = net_full * k
    cost_portion = kit_cost_full * k
    studio_fund = max(0.0, cost - cost_portion)
    if mutate_stock:
        kit.pieces_available = avail - n
        if kit.pieces_available <= 0:
            kit.is_in_stock = False
    return n, cost, studio_fund, {}


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
        if not user_has_role(db, mid, UserRole.MASTER):
            dn = (u.display_name or u.username or "").strip() or f"ID {mid}"
            raise ValueError(f"«{dn}» не в роли мастера.")
        seen.add(mid)
        out.append((mid, float(p)))
    if len(out) != len(allocations):
        raise ValueError("Дублирование мастера в списке долей не допускается.")
    return out


def _parse_visit_client_discount_percent(g: Callable[[str, str], str]) -> int:
    """Целые 0–100; пусто = 0. g — функция имя→строка как в parse_kit_inlay_form."""

    raw = (g("client_discount_percent", "") or "").strip()
    if not raw:
        return 0
    try:
        v = parse_float(raw, field_name="client_discount_percent")
    except ValueError:
        raise ValueError("Скидка клиенту: укажите целое число процентов от 0 до 100.")
    if v < 0 or v > 100:
        raise ValueError("Скидка клиенту — от 0 до 100%.")
    if abs(v - round(v)) > 1e-6:
        raise ValueError("Скидка клиенту указывается целым числом процентов.")
    return int(round(v))


def _parse_optional_nonneg_int(g: Callable[[str, str], str], name: str) -> int | None:
    raw = (g(name, "") or "").strip()
    if not raw:
        return None
    try:
        v = int(parse_float(raw, field_name=name))
    except ValueError:
        return None
    return max(0, v)


def _parse_stock_breakdown_json(g: Callable[[str, str], str], field: str) -> dict[str, int] | None:
    raw = (g(field, "") or "").strip()
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    out: dict[str, int] = {}
    for k, v in d.items():
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out[str(k).strip()] = n
    return out or None


def _usage_dict_from_json_val(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, int] = {}
    for k, v in raw.items():
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out[str(k).strip()] = n
    return out or None


@dataclass
class StockKitLineInput:
    kit_id: int
    use_entire: bool
    blanks_used: int
    usage_by_key: dict[str, int] | None = None
    # None = взять посчитанную цену списания; число = сумма с клиента за комплект.
    amount_from_client: float | None = None


def _parse_stock_kit_lines_from_form(
    form: Any,
    g: Callable[[str, str], str],
    g_int: Callable[[str, int], int],
    g_bool: Callable[[str], bool],
    *,
    lines_json_field: str = "stock_kit_lines_json",
    legacy_kit_id_field: str = "stock_kit_id",
    legacy_use_entire_field: str = "stock_use_entire",
    legacy_blanks_field: str = "stock_blanks_used",
    legacy_breakdown_field: str = "stock_breakdown_json",
) -> list[StockKitLineInput]:
    raw = (g(lines_json_field, "") or "").strip()
    lines: list[StockKitLineInput] = []
    if raw:
        try:
            arr = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Некорректные данные списка комплектов из наличия.") from exc
        if not isinstance(arr, list):
            raise ValueError("Ожидается список комплектов из наличия.")
        for item in arr:
            if not isinstance(item, dict):
                continue
            try:
                kid = int(item.get("kit_id") or 0)
            except (TypeError, ValueError):
                kid = 0
            if kid <= 0:
                continue
            ue = bool(item.get("use_entire"))
            try:
                bu = int(item.get("blanks_used") or 0)
            except (TypeError, ValueError):
                bu = 0
            bu = max(0, bu)
            ub = _usage_dict_from_json_val(item.get("breakdown"))
            afc_raw = item.get("amount_from_client")
            afc: float | None = None
            if afc_raw is not None and str(afc_raw).strip() != "":
                try:
                    afc = max(0.0, float(afc_raw))
                except (TypeError, ValueError):
                    afc = None
            lines.append(
                StockKitLineInput(
                    kit_id=kid,
                    use_entire=ue,
                    blanks_used=bu,
                    usage_by_key=ub,
                    amount_from_client=afc,
                )
            )
        if lines:
            return lines
    sid = g_int(legacy_kit_id_field, 0)
    if sid > 0:
        return [
            StockKitLineInput(
                kit_id=sid,
                use_entire=g_bool(legacy_use_entire_field),
                blanks_used=g_int(legacy_blanks_field, 0),
                usage_by_key=_parse_stock_breakdown_json(g, legacy_breakdown_field),
            )
        ]
    return []


def _own_extra_stock_lines_from_input(inp: "KitInlayFormInput") -> list[StockKitLineInput]:
    if inp.own_extra_stock_kit_lines:
        return inp.own_extra_stock_kit_lines
    if inp.own_extra_stock_kit_id:
        return [
            StockKitLineInput(
                kit_id=inp.own_extra_stock_kit_id,
                use_entire=inp.own_extra_stock_use_entire,
                blanks_used=inp.own_extra_stock_blanks_used,
                usage_by_key=inp.own_extra_stock_usage_by_key,
            )
        ]
    return []


def parse_kit_inlay_form(
    form: Any, *, single_master_default_id: int | None = None
) -> KitInlayFormInput:
    """Разбор `starlette.datastructures.FormData` после `await request.form()`.

    Если передан `single_master_default_id` и галочка «несколько мастеров» не отмечена,
    доля 100% уходит этому пользователю (остальные отметки в форме игнорируются).
    """

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
        raw = g(name, "").strip()
        if not raw:
            return default
        try:
            return int(parse_float(raw, field_name=name))
        except ValueError:
            return default

    def g_float(name: str, default: float = 0.0) -> float:
        raw = g(name, "").strip()
        if not raw:
            return default
        try:
            return parse_float(raw, default=default, field_name=name)
        except ValueError:
            return default

    def g_bool(name: str) -> bool:
        v = form.get(name)
        if v is None:
            return False
        if isinstance(v, UploadFile):
            return False
        s = v.decode() if isinstance(v, (bytes, bytearray)) else v
        return parse_bool(s)

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

    stock_kit_lines = _parse_stock_kit_lines_from_form(form, g, g_int, g_bool)
    stock_id = stock_kit_lines[0].kit_id if stock_kit_lines else 0
    own_extra_stock_kit_lines = _parse_stock_kit_lines_from_form(
        form,
        g,
        g_int,
        g_bool,
        lines_json_field="own_extra_stock_kit_lines_json",
        legacy_kit_id_field="own_extra_stock_kit_id",
        legacy_use_entire_field="own_extra_stock_use_entire",
        legacy_blanks_field="own_extra_stock_blanks_used",
        legacy_breakdown_field="own_extra_stock_breakdown_json",
    )
    extra_stock_id = (
        own_extra_stock_kit_lines[0].kit_id if own_extra_stock_kit_lines else g_int("own_extra_stock_kit_id", 0)
    )

    ct = VisitClientType.SELF if g_bool("client_is_self") else VisitClientType.RETURNING
    disc_pct = _parse_visit_client_discount_percent(g)

    pd_raw = g("performed_date", "")
    try:
        performed_date = parse_date_iso(pd_raw, field_name="performed_date") if pd_raw else date.today()
    except ValueError:
        performed_date = date.today()

    mode_raw = (g("client_mode", "existing") or "existing").lower()
    client_mode = "draft" if mode_raw == "draft" else "existing"
    eid = g_int("existing_client_id", 0)
    existing_client_id = eid if eid > 0 else None

    if single_master_default_id is not None and not g_bool("visit_use_multi_masters"):
        visit_master_allocations = [(single_master_default_id, 100)]
    else:
        # Без single_master_default (админ без роли мастера / редактирование) —
        # только явный выбор из списка.
        visit_master_allocations = _parse_visit_master_allocations_from_form(form)

    own_corr_use_custom = g_bool("own_corr_use_custom_amount")
    own_corr_custom_amt = max(0.0, g_float("own_corr_custom_amount", 0))
    if g_bool("own_correction") and not own_corr_use_custom and own_corr_custom_amt > 0:
        own_corr_use_custom = True
    own_corr_master_raw = g("own_corr_master_id", "").strip()
    own_corr_master_id = (
        int(own_corr_master_raw) if own_corr_master_raw.isdigit() and int(own_corr_master_raw) > 0 else None
    )
    visit_amount = g_float("amount_from_client", 0)

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
        client_discount_percent=disc_pct,
        performed_date=performed_date,
        duration_minutes=g_int("duration_h", 0) * 60 + g_int("duration_m", 0),
        amount_from_client=visit_amount,
        kanekalon_grams=kanekalon_grams,
        kudri_grams=kudri_grams,
        mix_source=mix,
        mix_complexity=comp,
        amortization_level=amort,
        service_id=g_int("service_id", 0),
        kit_kind=g("kit_kind", "STOCK").upper(),
        stock_kit_id=stock_id if stock_id else None,
        stock_use_entire=stock_kit_lines[0].use_entire if stock_kit_lines else False,
        stock_blanks_used=stock_kit_lines[0].blanks_used if stock_kit_lines else 0,
        stock_usage_by_key=stock_kit_lines[0].usage_by_key if stock_kit_lines else None,
        stock_kit_lines=stock_kit_lines,
        kit_paid_separately=g_bool("kit_paid_separately"),
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
        own_extra_stock_kit_lines=own_extra_stock_kit_lines,
        own_extra_stock_use_entire=(
            own_extra_stock_kit_lines[0].use_entire if own_extra_stock_kit_lines else g_bool("own_extra_stock_use_entire")
        ),
        own_extra_stock_blanks_used=(
            own_extra_stock_kit_lines[0].blanks_used if own_extra_stock_kit_lines else g_int("own_extra_stock_blanks_used", 0)
        ),
        own_extra_stock_usage_by_key=(
            own_extra_stock_kit_lines[0].usage_by_key
            if own_extra_stock_kit_lines
            else _parse_stock_breakdown_json(g, "own_extra_stock_breakdown_json")
        ),
        own_corr_trim_qty=g_int("own_corr_trim_qty", 0),
        own_corr_hourly_hours=max(0.0, g_float("own_corr_hourly_hours", 0)),
        own_corr_kit_description=g("own_corr_kit_description", ""),
        own_corr_kit_blanks_count=_parse_optional_nonneg_int(g, "own_corr_kit_blanks_count"),
        own_corr_wash=g_bool("own_corr_wash"),
        own_corr_circle=g_bool("own_corr_circle"),
        own_corr_steam=g_bool("own_corr_steam"),
        own_corr_use_custom_amount=own_corr_use_custom,
        own_corr_custom_amount=own_corr_custom_amt,
        own_corr_client_payment_kind=parse_client_payment_kind(g("own_corr_client_payment_kind", "")),
        own_corr_master_id=own_corr_master_id,
        client_payment_kind=parse_client_payment_kind(g("client_payment_kind", "")),
        visit_master_allocations=visit_master_allocations,
        questionnaire_raw=extract_questionnaire_raw_from_form(form),
        addon_sales_amount=max(0.0, g_float("addon_sales_amount", 0)),
        addon_sales_description=g("addon_sales_description", ""),
        thermo_parsed=parse_thermo_from_form(form),
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
    client_discount_percent: int
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
    stock_usage_by_key: dict[str, int] | None
    kit_paid_separately: bool
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
    own_extra_stock_usage_by_key: dict[str, int] | None
    own_corr_trim_qty: int
    own_corr_hourly_hours: float
    own_corr_kit_description: str
    own_corr_kit_blanks_count: int | None
    own_corr_wash: bool
    own_corr_circle: bool
    own_corr_steam: bool
    visit_master_allocations: list[tuple[int, int]]
    questionnaire_raw: dict[str, str]
    addon_sales_amount: float
    addon_sales_description: str
    thermo_parsed: ThermoFormParsed
    stock_kit_lines: list[StockKitLineInput] = field(default_factory=list)
    own_extra_stock_kit_lines: list[StockKitLineInput] = field(default_factory=list)
    own_corr_use_custom_amount: bool = False
    own_corr_custom_amount: float = 0.0
    own_corr_client_payment_kind: ClientPaymentKind = ClientPaymentKind.CASH
    own_corr_master_id: int | None = None
    client_payment_kind: ClientPaymentKind = ClientPaymentKind.CASH


def _answers_labels_display_from_specs(
    specs: list[MergedQuestionnaireFieldSpec], answers: dict[str, Any]
) -> tuple[dict[str, str], dict[str, str]]:
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
    return answer_labels, answer_display


def _build_kit_block_from_input(inp: KitInlayFormInput, db: Session) -> KitBlock:
    kind = inp.kit_kind.upper()
    if kind == "STOCK":
        lines = inp.stock_kit_lines
        if not lines:
            raise ValueError("Выберите комплект из наличия")
        from_stocks: list[KitFromStock] = []
        for line in lines:
            kit_row = _validate_stock_selection(
                db,
                kit_id=line.kit_id,
                use_entire=line.use_entire,
                blanks_used=line.blanks_used,
                client_id=inp.existing_client_id,
                usage_by_key=line.usage_by_key,
            )
            from_stocks.append(
                KitFromStock(
                    sku=kit_row.sku,
                    blanks_used=0 if line.use_entire else line.blanks_used,
                    use_entire_kit=line.use_entire,
                    usage_by_key=line.usage_by_key,
                )
            )
        return KitBlock(
            kind="STOCK",
            from_stock=from_stocks[0],
            from_stocks=from_stocks,
            new_kit=None,
            own=None,
        )
    if kind == "NEW":
        raise ValueError("Режим «Новый комплект» временно отключён. Выберите «Из наличия» или «Свой».")
    if kind == "OWN":
        if inp.own_origin not in ("STUDIO", "FOREIGN"):
            raise ValueError("Укажите происхождение своего комплекта")
        corr_details: KitOwnCorrectionDetails | None = None
        if inp.own_correction:
            if inp.own_corr_wash and inp.own_corr_circle:
                raise ValueError(
                    "Если выбрана «Стирка» (коррекция), то «Одевание на круг» выбирать нельзя (входит в стирку)."
                )
            tq = max(0, int(inp.own_corr_trim_qty))
            hh = max(0.0, float(inp.own_corr_hourly_hours))
            desc = (inp.own_corr_kit_description or "").strip()
            kb = inp.own_corr_kit_blanks_count
            if kb is not None and kb < 0:
                raise ValueError("«Количество заготовок в комплекте» не может быть отрицательным.")
            corr_details = KitOwnCorrectionDetails(
                trim_qty=tq,
                hourly_hours=hh,
                kit_description=desc,
                kit_blanks_count=kb,
                wash=inp.own_corr_wash,
                circle=inp.own_corr_circle,
                steam=inp.own_corr_steam,
                use_custom_amount=bool(inp.own_corr_use_custom_amount),
                custom_amount=(
                    float(inp.own_corr_custom_amount)
                    if inp.own_corr_use_custom_amount and float(inp.own_corr_custom_amount or 0) > 0
                    else None
                ),
                master_id=inp.own_corr_master_id,
            )
        extra: KitOwnExtra | None = None
        if inp.own_extra_blanks:
            extra_lines = _own_extra_stock_lines_from_input(inp)
            if not extra_lines:
                raise ValueError("Выберите комплект для доп. заготовок")
            from_stocks_extra: list[KitFromStock] = []
            for line in extra_lines:
                ek = _validate_stock_selection(
                    db,
                    kit_id=line.kit_id,
                    use_entire=line.use_entire,
                    blanks_used=line.blanks_used,
                    client_id=inp.existing_client_id,
                    usage_by_key=line.usage_by_key,
                )
                from_stocks_extra.append(
                    KitFromStock(
                        sku=ek.sku,
                        blanks_used=0 if line.use_entire else line.blanks_used,
                        use_entire_kit=line.use_entire,
                        usage_by_key=line.usage_by_key,
                    )
                )
            extra = KitOwnExtra(
                source="STOCK",
                from_stock=from_stocks_extra[0],
                from_stocks=from_stocks_extra,
                new_kit=None,
            )
        return KitBlock(
            kind="OWN",
            from_stock=None,
            new_kit=None,
            own=KitOwn(
                origin=inp.own_origin,  # type: ignore[arg-type]
                correction=inp.own_correction,
                correction_details=corr_details,
                extra_blanks=inp.own_extra_blanks,
                extra=extra,
            ),
        )
    raise ValueError("Неверный тип комплекта")


def build_payload_from_input(inp: KitInlayFormInput, db: Session) -> VisitServiceDetailsPayload:
    service = db.scalar(
        select(Service)
        .options(selectinload(Service.subcategory))
        .where(Service.id == inp.service_id, Service.is_active.is_(True))
    )
    if not service or not service.subcategory:
        raise ValueError("Услуга не найдена")

    if service_requires_thermo_flow(service):
        thermo = build_thermo_visit_details(
            inp.thermo_parsed,
            db,
            client_id=inp.existing_client_id,
        )
        return parse_visit_service_details(
            {
                "service_fields": {},
                "kit": None,
                "answers": {},
                "answer_labels": {},
                "answer_display": {},
                "thermo": thermo.model_dump(mode="json"),
            }
        )

    specs = load_merged_questionnaire_specs(db, inp.service_id)
    answers, q_errors = validate_and_coerce_answers(inp.questionnaire_raw, specs)
    if q_errors:
        raise ValueError("; ".join(q_errors))

    answer_labels, answer_display = _answers_labels_display_from_specs(specs, answers)

    kit_block = None
    if effective_requires_kit_block(service, answers, specs):
        kit_block = _build_kit_block_from_input(inp, db)

    thermo_dump = None
    if effective_requires_thermo_block(service, answers, specs) and not service_requires_thermo_flow(service):
        # Галочка открыла термо поверх обычной анкеты (не полный thermo-only flow).
        if inp.thermo_parsed is not None:
            thermo = build_thermo_visit_details(
                inp.thermo_parsed,
                db,
                client_id=inp.existing_client_id,
            )
            thermo_dump = thermo.model_dump(mode="json")

    payload_data: dict[str, Any] = {
        "service_fields": {},
        "kit": kit_block.model_dump(mode="json") if kit_block is not None else None,
        "answers": answers,
        "answer_labels": answer_labels,
        "answer_display": answer_display,
    }
    if thermo_dump is not None:
        payload_data["thermo"] = thermo_dump
    return parse_visit_service_details(payload_data)


def validate_master_visit_step1(db: Session, inp: KitInlayFormInput) -> None:
    """Проверки до перехода на шаг с вопросами анкеты (без ответов q_*)."""
    if inp.service_id <= 0:
        raise ValueError("Выберите услугу")

    service = db.scalar(
        select(Service)
        .options(selectinload(Service.subcategory).selectinload(ServiceSubcategory.category))
        .where(Service.id == inp.service_id, Service.is_active.is_(True))
    )
    if not service or not service.subcategory or not service.subcategory.category:
        raise ValueError("Услуга не найдена")
    if (service.subcategory.category.name or "").strip() in ("Заказ", "Продажа материала"):
        raise ValueError("Эта позиция недоступна для выбора в визите")

    _resolve_visit_master_allocations(db, inp.visit_master_allocations)

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
    else:
        if not inp.existing_client_id:
            raise ValueError("Найдите и выберите клиента из списка или переключитесь на «Новый черновик».")

    grams_total = max(0.0, inp.kanekalon_grams) + max(0.0, inp.kudri_grams)
    if grams_total > 0 and inp.mix_source and inp.mix_source != MixSource.NO_MIX:
        if inp.mix_complexity is None:
            raise ValueError("Укажите сложность смешки")

    if inp.client_type != VisitClientType.SELF and inp.amount_from_client <= 0:
        has_stock_kit = (
            (inp.kit_kind or "").upper() == "STOCK"
            and bool(inp.stock_kit_lines)
            and not bool(inp.kit_paid_separately)
        )
        if not has_stock_kit:
            raise ValueError("Укажите сумму с клиента за услугу и/или за комплект.")
    if inp.client_discount_percent < 0 or inp.client_discount_percent > 100:
        raise ValueError("Скидка клиенту — от 0 до 100%.")

    specs = load_merged_questionnaire_specs(db, int(service.id))
    answers, _q_errs = validate_and_coerce_answers(inp.questionnaire_raw or {}, specs)
    if effective_requires_kit_block(service, answers, specs):
        _build_kit_block_from_input(inp, db)


def collect_step1_fields_for_step2_hidden(form: Any) -> dict[str, list[str]]:
    """Поля шага 1 для hidden inputs на шаге 2 (без ответов анкеты q_*)."""
    out: dict[str, list[str]] = {}
    for key in form.keys():
        if key.startswith("q_"):
            continue
        vals: list[str] = []
        for v in form.getlist(key):
            if isinstance(v, UploadFile):
                continue
            s = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
            vals.append(s)
        if vals:
            out[key] = vals
    return out


def collect_questionnaire_prefill_from_form(form: Any) -> dict[str, str]:
    """Поля q_* для повторного показа шага 2 после ошибки."""
    out: dict[str, str] = {}
    for k in form.keys():
        if not isinstance(k, str) or not k.startswith("q_"):
            continue
        vs = [v for v in form.getlist(k) if not isinstance(v, UploadFile)]
        if not vs:
            continue
        v = vs[-1]
        out[k] = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
    return out


def master_visit_step1_prefill_from_form(form: Any) -> tuple[dict[str, str], list[int], dict[int, str]]:
    """Восстановление шага 1 после «Назад» с шага 2."""
    vm_on_ids, vm_pct_str = read_visit_master_form_state(form)
    fp: dict[str, str] = {}
    for key in form.keys():
        if key == "visit_master_on" or key.startswith("visit_master_pct_"):
            continue
        if key.startswith("q_"):
            continue
        last: str | None = None
        for v in form.getlist(key):
            if isinstance(v, UploadFile):
                continue
            last = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
        if last is not None:
            fp[key] = last
    return fp, vm_on_ids, vm_pct_str


def save_kit_inlay_visit(
    db: Session,
    master_id: int,
    inp: KitInlayFormInput,
    *,
    created_by_label: str | None = None,
) -> Visit:
    """Визит с выбранным клиентом или новым черновиком; услуга, склад STOCK, расчёт."""
    from app.visit_multi_service import kit_inlay_to_multi, save_visit_with_services

    return save_visit_with_services(
        db,
        master_id,
        kit_inlay_to_multi(inp),
        created_by_label=created_by_label,
    )


def list_master_visit_services(db: Session) -> list[Service]:
    # Категории "Заказ"/"Продажа материала" — отдельные потоки, не в форме визита.
    _EXCLUDED_CATS = ("Заказ", "Продажа материала")
    return list(
        db.scalars(
            select(Service)
            .options(selectinload(Service.subcategory))
            .join(ServiceSubcategory, Service.subcategory_id == ServiceSubcategory.id)
            .join(ServiceCategory, ServiceSubcategory.category_id == ServiceCategory.id)
            .where(
                Service.is_active.is_(True),
                ServiceCategory.is_active.is_(True),
                ServiceCategory.name.not_in(_EXCLUDED_CATS),
            )
            .order_by(
                ServiceCategory.name.asc(),
                ServiceSubcategory.name.asc(),
                Service.name.asc(),
            )
        ).all()
    )


def list_master_visit_services_catalog(db: Session) -> list[dict[str, Any]]:
    """Категория → подкатегория → услуга для формы визита (все активные категории, кроме отдельных потоков)."""

    def _opt_f(v: float | None) -> float | None:
        return None if v is None else float(v)

    services = list_master_visit_services(db)
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
                "estimated_duration_minutes": int(s.estimated_duration_minutes or 0),
                "requires_kit_block": service_requires_kit_block(s),
                "requires_tail_block": service_requires_tail_block(s),
                "requires_thermo": service_requires_thermo_flow(s),
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


def list_kit_inlay_services_catalog(db: Session) -> list[dict[str, Any]]:
    """Совместимость: то же, что полный каталог для визита."""
    return list_master_visit_services_catalog(db)


def list_kits_for_stock(db: Session) -> list[Kit]:
    return list(
        db.scalars(
            select(Kit)
            .where(
                Kit.is_archived.is_(False),
                Kit.is_active.is_(True),
                Kit.pieces_available > 0,
            )
            .options(
                selectinload(Kit.reserves).selectinload(KitReserve.reserved_for_client),
                selectinload(Kit.reserves).selectinload(KitReserve.reserved_for_user),
            )
            .order_by(Kit.sku.asc())
        ).all()
    )


def kit_reserved_for_visit_label(kit: Kit) -> str | None:
    """Краткое описание резервов в анкете визита (без даты и автора)."""
    rows = list(kit.reserves or [])
    if not rows:
        return None
    bits = [f"{r.pieces_reserved} шт. — {_kit_reserve_target_short(r)}" for r in rows]
    return "; ".join(bits)


def _per_key_condition_meta(kit: Kit) -> dict[str, dict[str, Any]]:
    """Состояние и % б/у по ключу заготовки (для таблицы списания в визите/брони)."""
    from collections import defaultdict

    from app.kit_composition_lines import BlankCondition, lines_from_json

    by_key: dict[str, list[Any]] = defaultdict(list)
    for ln in lines_from_json(kit.composition_json):
        if ln.key:
            by_key[str(ln.key)].append(ln)
    fallback = str(getattr(kit, "blanks_condition", None) or "NEW").upper()
    out: dict[str, dict[str, Any]] = {}
    for kk, lns in by_key.items():
        conds = {ln.condition for ln in lns}
        if BlankCondition.USED in conds and BlankCondition.NEW in conds:
            cond = "MIXED"
        elif BlankCondition.USED in conds:
            cond = "USED"
        else:
            cond = "NEW"
        pct: int | None = None
        used_lns = [ln for ln in lns if ln.condition == BlankCondition.USED]
        if used_lns:
            pcts = [int(ln.used_price_pct or 100) for ln in used_lns]
            if len(set(pcts)) == 1:
                pct = pcts[0]
        out[kk] = {"condition": cond, "used_price_pct": pct}
    if not out and fallback in ("NEW", "USED", "MIXED"):
        out["*"] = {"condition": fallback, "used_price_pct": None}
    return out


def kit_suggest_dict_for_kit(db: Session, k: Kit, *, for_client_id: int | None) -> dict[str, Any]:
    """Одна строка подсказки «из наличия» (как в suggest_kits_for_stock)."""
    cid = int(for_client_id) if for_client_id is not None and int(for_client_id) > 0 else None
    res_label = kit_reserved_for_visit_label(k)
    sp = k.stock_price_total
    has_price = sp is not None and float(sp) > 0
    dp = int(k.discount_percent or 0)
    reserved_for_c = 0
    if cid is not None:
        for r in k.reserves or []:
            if (r.reserved_for_client_id or 0) == cid:
                reserved_for_c += int(r.pieces_reserved or 0)
    out: dict[str, Any] = {
        "id": k.id,
        "sku": k.sku,
        "title": k.title,
        "pieces_total": int(k.pieces_total or 0),
        "pieces_available": k.pieces_available,
        "reserved_for_selected_client": reserved_for_c,
        "stock_price_total": float(sp or 0.0),
        "discount_percent": dp,
        "missing_sale_price": not has_price,
        "is_reserved": bool(k.reserves),
        "reserved_for_label": res_label,
        "inventory_keyed": kit_inventory_is_keyed(db, int(k.id)),
        "per_key": [],
        "composition_requires_blank_stock": False,
    }
    comp = parse_composition_totals(k)
    if comp and int(k.pieces_available or 0) > 0 and not kit_inventory_is_keyed(db, int(k.id)):
        out["composition_requires_blank_stock"] = True
    csum = sum(max(0, int(v)) for v in comp.values()) if comp else 0
    out["composition_sum"] = int(csum) if csum > 0 else 0
    out["cost_total"] = float(k.cost_total or 0.0)
    if not out["inventory_keyed"]:
        return out
    sm = blank_stock_qty_map(db, int(k.id))
    price_map, _meta, label_by_key = load_catalog_kit_maps(db)
    max_by = max_take_by_key_for_client(db, kit=k, client_id=cid, stock_map=sm)
    cond_meta = _per_key_condition_meta(k)
    hints: list[dict[str, Any]] = []
    for kk in sorted(sm.keys()):
        p = price_map.get(kk)
        cm = cond_meta.get(kk) or cond_meta.get("*") or {}
        hints.append(
            {
                "key": kk,
                "qty_free": int(sm.get(kk, 0)),
                "qty_max_for_client": int(max_by.get(kk, 0)),
                "price_per_piece": float(p) if p is not None else None,
                "label": label_by_key.get(kk, kk),
                "composition_qty": int(comp.get(kk, 0)),
                "condition": str(cm.get("condition") or "NEW"),
                "used_price_pct": cm.get("used_price_pct"),
            }
        )
    out["per_key"] = hints
    return out


def kit_suggest_dict_for_kit_id(
    db: Session, kit_id: int, *, for_client_id: int | None
) -> dict[str, Any] | None:
    """Загрузка комплекта по id для префилла формы визита (предпросмотр себестоимости)."""
    k = db.scalar(
        select(Kit)
        .where(Kit.id == kit_id)
        .options(
            selectinload(Kit.reserves).selectinload(KitReserve.reserved_for_client),
            selectinload(Kit.reserves).selectinload(KitReserve.reserved_for_user),
        )
    )
    if not k:
        return None
    return kit_suggest_dict_for_kit(db, k, for_client_id=for_client_id)


def kit_reserve_hint_by_id(db: Session, kit_id: int | None) -> str | None:
    if not kit_id:
        return None
    k = db.scalar(
        select(Kit)
        .options(
            selectinload(Kit.reserves).selectinload(KitReserve.reserved_for_client),
            selectinload(Kit.reserves).selectinload(KitReserve.reserved_for_user),
        )
        .where(Kit.id == kit_id)
    )
    if not k:
        return None
    return kit_reserved_for_visit_label(k)


def suggest_kits_for_stock(
    db: Session, q: str, *, limit: int = 30, for_client_id: int | None = None
) -> list[dict[str, Any]]:
    """Подсказки для мастера: комплекты в наличии, фильтр по артикулу или названию.

    Если передан ``for_client_id``, в выборку попадают также комплекты с нулевым свободным
    остатком, но с активным резервом именно на этого клиента (ручной резерв со склада).
    Такие позиции сортируются в начало списка (по объёму резерва), при пустом запросе
    лимит строк чуть увеличивается, чтобы резервы не «вытеснялись» комплектами только из наличия.
    """
    needle = (q or "").strip()
    cid = int(for_client_id) if for_client_id is not None and int(for_client_id) > 0 else None
    stock_clause = Kit.pieces_available > 0
    if cid is not None:
        reserve_for_client = exists().where(
            KitReserve.kit_id == Kit.id,
            KitReserve.reserved_for_client_id == cid,
            KitReserve.pieces_reserved > 0,
        )
        stock_clause = or_(Kit.pieces_available > 0, reserve_for_client)
    stmt = (
        select(Kit)
        .where(
            Kit.is_archived.is_(False),
            Kit.is_active.is_(True),
            stock_clause,
        )
        .options(
            selectinload(Kit.reserves).selectinload(KitReserve.reserved_for_client),
            selectinload(Kit.reserves).selectinload(KitReserve.reserved_for_user),
        )
    )
    if needle:
        stmt = stmt.where(
            or_(Kit.sku.ilike(f"%{needle}%"), Kit.title.ilike(f"%{needle}%"))
        )
    # Пустой запрос + клиент: нужно уместить и «просто в наличии», и «только в резерве» — чуть шире лимит.
    eff_limit = max(limit, 60) if (cid is not None and not needle) else limit
    stmt = stmt.order_by(Kit.sku.asc()).limit(eff_limit)
    rows = list(db.scalars(stmt).all())
    out: list[dict[str, Any]] = []
    for k in rows:
        out.append(kit_suggest_dict_for_kit(db, k, for_client_id=cid))
    if cid is not None:
        out.sort(
            key=lambda d: (
                -int(d.get("reserved_for_selected_client") or 0),
                (d.get("sku") or "").lower(),
            )
        )
    return out
