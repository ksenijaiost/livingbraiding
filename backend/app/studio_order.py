"""
Заказ (категория «Заказ»): отдельная форма, расчёт как у визита, доли — все роли сотрудников.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import UploadFile

from app.auth import AuthUser, require_role
from app.client_validation import (
    client_has_any_contact,
    format_created_by_label,
    strip_or_none,
)
from app.db.session import get_db
from app.db.models import (
    AmortizationLevel,
    Client,
    Kit,
    MixComplexity,
    MixSource,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    StudioOrder,
    StudioOrderKitUsage,
    StudioOrderServiceLine,
    StudioOrderStaff,
    StudioOrderSubcategoryKey,
    User,
    UserRole,
    VisitClientType,
    VisitPriceType,
)
from app.kit_crud import (
    apply_kit_admin_form,
    list_masters_for_kit_author_pick,
    parse_kit_admin_form,
    sync_kit_authors,
    validate_kit_admin_form,
)
from app.kit_inlay_visit import (
    _apply_stock_kit_usage,
    _materials_cost_and_snapshot,
    _validate_stock_selection,
    get_salon_cut_pct,
)

ZAKAZ_CATEGORY = "Заказ"
SUB_KOMPLEKT = "Комплект"
SUB_ZAGOTOVKI = "Заготовки поштучно"
SUB_REZINKI = "Резинки"
SUB_KORREKTSIYA = "Коррекция комплекта"


class PrefixedFormView:
    """Оборачивает form-data с префиксом полей (новый комплект в заказе)."""

    def __init__(self, form: Any, prefix: str):
        self._form = form
        self._prefix = prefix

    def get(self, name: str, default: str | None = None):
        v = self._form.get(self._prefix + name)
        if v is None:
            return default
        if isinstance(v, UploadFile):
            return default
        if isinstance(v, (bytes, bytearray)):
            return v.decode().strip()
        return str(v).strip()

    def getlist(self, name: str) -> list[Any]:
        if not hasattr(self._form, "getlist"):
            v = self._form.get(self._prefix + name)
            return [v] if v is not None else []
        return list(self._form.getlist(self._prefix + name))


def _g_str(form: Any, name: str, default: str = "") -> str:
    v = form.get(name)
    if v is None or isinstance(v, UploadFile):
        return default
    if isinstance(v, (bytes, bytearray)):
        return v.decode().strip()
    return str(v).strip()


def _g_int(form: Any, name: str, default: int = 0) -> int:
    try:
        return int(_g_str(form, name, str(default)) or str(default))
    except ValueError:
        return default


def _g_float(form: Any, name: str, default: float = 0.0) -> float:
    try:
        return float((_g_str(form, name, str(default)) or str(default)).replace(",", "."))
    except ValueError:
        return default


def _g_bool(form: Any, name: str) -> bool:
    v = form.get(name)
    if v is None or isinstance(v, UploadFile):
        return False
    s = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
    return s.lower() in ("on", "true", "1", "yes")


def read_order_staff_form_state(form: Any) -> tuple[list[int], dict[int, str]]:
    raw: list[Any] = []
    if hasattr(form, "getlist"):
        raw = list(form.getlist("order_staff_on"))
    else:
        v = form.get("order_staff_on")
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
        pct_str[mid] = _field_str(f"order_staff_pct_{mid}")
    return active_ids, pct_str


def parse_order_staff_allocations_from_form(form: Any) -> list[tuple[int, int]]:
    active_ids, pct_str = read_order_staff_form_state(form)
    if not active_ids:
        raise ValueError("Отметьте хотя бы одного сотрудника и укажите доли в процентах.")
    rows: list[tuple[int, int]] = []
    if len(active_ids) == 1:
        mid = active_ids[0]
        s = pct_str.get(mid, "").strip()
        p = 100 if not s else int(s)
        rows.append((mid, p))
        return rows
    for mid in active_ids:
        s = pct_str.get(mid, "").strip()
        if not s:
            raise ValueError("Для каждого отмеченного сотрудника укажите целый процент.")
        rows.append((mid, int(s)))
    return rows


def resolve_order_staff_allocations(
    db: Session, allocations: list[tuple[int, int]]
) -> list[tuple[int, float]]:
    total = sum(p for _, p in allocations)
    if total != 100:
        raise ValueError("Сумма долей сотрудников должна быть ровно 100%.")
    allowed = (UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)
    seen: set[int] = set()
    out: list[tuple[int, float]] = []
    for mid, p in allocations:
        if p < 0 or p > 100:
            raise ValueError("Процент каждого сотрудника должен быть от 0 до 100.")
        if mid in seen:
            continue
        u = db.get(User, mid)
        if not u or not u.is_active:
            raise ValueError(f"Сотрудник (ID {mid}) не найден или отключён.")
        if u.role not in allowed:
            raise ValueError("В заказе можно указывать только мастеров и админов студии.")
        seen.add(mid)
        out.append((mid, float(p)))
    if len(out) != len(allocations):
        raise ValueError("Дублирование сотрудника в списке долей не допускается.")
    return out


def list_staff_for_order_form(db: Session) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .where(
                User.is_active.is_(True),
                User.role.in_((UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)),
            )
            .order_by(User.display_name.asc(), User.id.asc())
        ).all()
    )


def load_zakaz_catalog(db: Session) -> list[dict[str, Any]]:
    """Категория «Заказ» (include_in_visit=false): подкатегории и услуги."""
    cat = db.scalar(
        select(ServiceCategory)
        .where(ServiceCategory.name == ZAKAZ_CATEGORY, ServiceCategory.is_active.is_(True))
    )
    if not cat:
        return []
    subs = list(
        db.scalars(
            select(ServiceSubcategory)
            .where(
                ServiceSubcategory.category_id == cat.id,
                ServiceSubcategory.is_active.is_(True),
            )
            .order_by(ServiceSubcategory.name.asc())
        ).all()
    )
    out: list[dict[str, Any]] = []
    for sub in subs:
        svcs = list(
            db.scalars(
                select(Service)
                .where(Service.subcategory_id == sub.id, Service.is_active.is_(True))
                .order_by(Service.name.asc())
            ).all()
        )
        out.append({"id": sub.id, "name": sub.name, "services": svcs})
    return out


def _service_in_sub(db: Session, service_id: int, sub_name: str) -> Service:
    s = db.scalar(
        select(Service)
        .options(selectinload(Service.subcategory).selectinload(ServiceSubcategory.category))
        .where(Service.id == service_id, Service.is_active.is_(True))
    )
    if (
        not s
        or not s.subcategory
        or not s.subcategory.category
        or s.subcategory.category.name != ZAKAZ_CATEGORY
        or s.subcategory.name != sub_name
    ):
        raise ValueError("Некорректная услуга для выбранного типа заказа.")
    return s


def _rubber_tail_braid_ids(db: Session) -> tuple[int | None, int | None]:
    sub = db.scalar(
        select(ServiceSubcategory)
        .join(ServiceCategory, ServiceSubcategory.category_id == ServiceCategory.id)
        .where(
            ServiceCategory.name == ZAKAZ_CATEGORY,
            ServiceSubcategory.name == SUB_REZINKI,
        )
    )
    if not sub:
        return None, None
    tail = db.scalar(
        select(Service.id).where(
            Service.subcategory_id == sub.id,
            Service.name == "Прикрепление хвоста",
            Service.is_active.is_(True),
        )
    )
    braid = db.scalar(
        select(Service.id).where(
            Service.subcategory_id == sub.id,
            Service.name == "Брейд под хвост",
            Service.is_active.is_(True),
        )
    )
    return tail, braid


def _parse_kit_stock_from_form(form: Any, prefix: str) -> tuple[int | None, bool, int]:
    kid = _g_int(form, f"{prefix}stock_kit_id", 0)
    return (
        kid if kid > 0 else None,
        _g_bool(form, f"{prefix}stock_use_entire"),
        _g_int(form, f"{prefix}stock_blanks_used", 0),
    )


def _kit_block_stock_json(db: Session, kit_id: int, use_entire: bool, blanks_used: int) -> dict[str, Any]:
    kit = _validate_stock_selection(
        db, kit_id=kit_id, use_entire=use_entire, blanks_used=blanks_used
    )
    return {
        "kind": "STOCK",
        "from_stock": {
            "sku": kit.sku,
            "blanks_used": 0 if use_entire else blanks_used,
            "use_entire_kit": use_entire,
        },
        "new_kit": None,
        "own": None,
    }


def _create_kit_from_prefixed_form(db: Session, form: Any, prefix: str) -> Kit:
    pf = PrefixedFormView(form, prefix)
    d = parse_kit_admin_form(pf, for_create=True)
    validate_kit_admin_form(d, for_create=True)
    if db.scalar(select(Kit.id).where(Kit.sku == d.sku)):
        raise ValueError(f"Комплект с артикулом «{d.sku}» уже есть — укажите другой артикул.")
    kit = Kit()
    apply_kit_admin_form(kit, d)
    db.add(kit)
    db.flush()
    sync_kit_authors(db, kit, pf)
    return kit


def _parse_common_order(form: Any) -> dict[str, Any]:
    kanekalon_grams = _g_float(form, "kanekalon_grams", 0)
    kudri_grams = _g_float(form, "kudri_grams", 0)
    grams_total = max(0.0, kanekalon_grams) + max(0.0, kudri_grams)
    mix_raw = _g_str(form, "mix_source", "")
    if grams_total <= 0:
        mix = MixSource.NO_MIX
    else:
        try:
            mix = MixSource(mix_raw) if mix_raw else MixSource.NO_MIX
        except ValueError:
            mix = MixSource.NO_MIX
    comp: MixComplexity | None = None
    if grams_total > 0 and mix != MixSource.NO_MIX:
        cr = _g_str(form, "mix_complexity", "")
        try:
            comp = MixComplexity(cr) if cr else None
        except ValueError:
            comp = None

    ct = VisitClientType.SELF if _g_bool(form, "client_is_self") else VisitClientType.RETURNING
    try:
        pt = VisitPriceType(_g_str(form, "price_type", "CLIENT"))
    except ValueError:
        pt = VisitPriceType.CLIENT
    pd_raw = _g_str(form, "performed_date", "")
    try:
        performed_date = date.fromisoformat(pd_raw) if pd_raw else date.today()
    except ValueError:
        performed_date = date.today()
    mode_raw = (_g_str(form, "client_mode", "existing") or "existing").lower()
    client_mode = "draft" if mode_raw == "draft" else "existing"
    eid = _g_int(form, "existing_client_id", 0)
    existing_client_id = eid if eid > 0 else None

    return {
        "client_mode": client_mode,
        "existing_client_id": existing_client_id,
        "draft_name": _g_str(form, "draft_client_name"),
        "draft_phone": _g_str(form, "draft_phone"),
        "draft_telegram": _g_str(form, "draft_telegram"),
        "draft_vk": _g_str(form, "draft_vk"),
        "draft_instagram": _g_str(form, "draft_instagram"),
        "draft_other_contact": _g_str(form, "draft_other_contact"),
        "client_type": ct,
        "price_type": pt,
        "performed_date": performed_date,
        "amount_from_client": max(0.0, _g_float(form, "amount_from_client", 0)),
        "order_comment": _g_str(form, "order_comment"),
        "kanekalon_grams": kanekalon_grams,
        "kudri_grams": kudri_grams,
        "mix_source": mix,
        "mix_complexity": comp,
        "addon_sales_amount": max(0.0, _g_float(form, "addon_sales_amount", 0)),
        "addon_sales_description": _g_str(form, "addon_sales_description", ""),
    }


def _resolve_client(db: Session, common: dict[str, Any], *, created_by_label: str | None) -> Client:
    if common["client_mode"] == "draft":
        if not common["draft_name"].strip():
            raise ValueError("Укажите имя клиента для черновика.")
        if not client_has_any_contact(
            common["draft_phone"],
            common["draft_telegram"],
            common["draft_vk"],
            common["draft_instagram"],
            common["draft_other_contact"],
        ):
            raise ValueError("Для черновика нужен хотя бы один контакт.")
        c = Client(
            name=common["draft_name"].strip()[:200],
            phone=strip_or_none(common["draft_phone"], 30),
            telegram=strip_or_none(common["draft_telegram"], 100),
            vk=strip_or_none(common["draft_vk"], 120),
            instagram=strip_or_none(common["draft_instagram"], 120),
            other_contact=strip_or_none(common["draft_other_contact"], 200),
            comment=None,
            is_confirmed=False,
            created_by_label=created_by_label,
        )
        db.add(c)
        db.flush()
        return c
    if not common["existing_client_id"]:
        raise ValueError("Выберите клиента из базы.")
    c = db.get(Client, common["existing_client_id"])
    if not c:
        raise ValueError("Клиент не найден.")
    return c


def save_studio_order_from_form(
    db: Session,
    *,
    form: Any,
    created_by_user_id: int,
    created_by_label: str | None,
) -> StudioOrder:
    sub_raw = (_g_str(form, "order_subcategory") or "").strip().upper()
    try:
        sub_key = StudioOrderSubcategoryKey(sub_raw)
    except ValueError:
        raise ValueError("Выберите тип заказа (подкатегория).")

    common = _parse_common_order(form)
    grams_total = max(0.0, common["kanekalon_grams"]) + max(0.0, common["kudri_grams"])
    if grams_total > 0 and common["mix_source"] != MixSource.NO_MIX and common["mix_complexity"] is None:
        raise ValueError("Укажите сложность смешки.")
    if common["client_type"] != VisitClientType.SELF and common["amount_from_client"] <= 0:
        raise ValueError("Укажите сумму, взятую с клиента.")

    if _g_bool(form, "order_use_multi_staff"):
        staff_alloc = resolve_order_staff_allocations(
            db, parse_order_staff_allocations_from_form(form)
        )
    else:
        staff_alloc = [(created_by_user_id, 100.0)]

    client = _resolve_client(db, common, created_by_label=created_by_label)
    performed_dt = datetime.combine(common["performed_date"], datetime.min.time())

    mat_cost, k_snap, ku_snap = _materials_cost_and_snapshot(
        db,
        kanekalon_grams=common["kanekalon_grams"],
        kudri_grams=common["kudri_grams"],
    )
    salon_pct = get_salon_cut_pct(db)

    mix_cost = 0.0
    mix_bonus_amount = 0.0
    mix_bonus_master_id = None
    if common["mix_source"] and common["mix_source"] != MixSource.NO_MIX:
        if common["mix_complexity"] is None:
            raise ValueError("Укажите сложность смешки")
        coef = {"SIMPLE": 1.0, "MEDIUM": 1.5, "HARD": 2.0}[common["mix_complexity"].value]
        mix_cost = grams_total * coef
        if common["mix_source"] == MixSource.SELF_MIXED:
            mix_bonus_amount = mix_cost
            mix_bonus_master_id = created_by_user_id

    addons = max(0.0, float(common["addon_sales_amount"] or 0.0))
    addons_detail: dict[str, Any] = {}
    ad = (common["addon_sales_description"] or "").strip()
    if ad:
        addons_detail["description"] = ad
    addons_details_json = json.dumps(addons_detail, ensure_ascii=False) if addons_detail else None

    kit_cost_total = 0.0
    kit_studio_fund = 0.0
    kit_usages: list[tuple[int, int, float]] = []
    service_lines: list[tuple[Service, int, dict[str, Any]]] = []

    duration_minutes = 0
    amort_level: AmortizationLevel | None = None
    amort_amount = 0.0
    rubber_length = rubber_weight = None
    rubber_blanks = None
    rubber_desc = None
    kor_blanks = None
    kor_comment = None

    sort_i = 0

    if sub_key == StudioOrderSubcategoryKey.KOMPLEKT:
        sid = _g_int(form, "komplekt_service_id", 0)
        if not sid:
            raise ValueError("Выберите услугу (комплект).")
        svc = _service_in_sub(db, sid, SUB_KOMPLEKT)
        mode = _g_str(form, "komplekt_kit_mode", "STOCK").upper()
        details: dict[str, Any] = {}
        if mode == "STOCK":
            sk, ue, bu = _parse_kit_stock_from_form(form, "komplekt_")
            if not sk:
                raise ValueError("Выберите комплект из наличия.")
            details["kit"] = _kit_block_stock_json(db, sk, ue, bu)
            n, cost, sf = _apply_stock_kit_usage(db, kit_id=sk, use_entire=ue, blanks_used=bu)
            kit_usages.append((sk, n, cost))
            kit_cost_total += cost
            kit_studio_fund += sf
        elif mode == "NEW":
            kit = _create_kit_from_prefixed_form(db, form, "komplekt_new_")
            n, cost, sf = _apply_stock_kit_usage(
                db,
                kit_id=kit.id,
                use_entire=True,
                blanks_used=0,
            )
            details["kit"] = {
                "kind": "STOCK",
                "from_stock": {
                    "sku": kit.sku,
                    "blanks_used": n,
                    "use_entire_kit": True,
                },
                "new_kit": None,
                "own": None,
            }
            kit_usages.append((kit.id, n, cost))
            kit_cost_total += cost
            kit_studio_fund += sf
        else:
            raise ValueError("Укажите комплект: из наличия или новый.")
        service_lines.append((svc, sort_i, details))
        sort_i += 1

    elif sub_key == StudioOrderSubcategoryKey.ZAGOTOVKI:
        z_sub = next((x for x in load_zakaz_catalog(db) if x["name"] == SUB_ZAGOTOVKI), None)
        if not z_sub:
            raise ValueError("В каталоге нет подкатегории «Заготовки поштучно».")
        picked_any = False
        for svc in z_sub["services"]:
            if not _g_bool(form, f"z_on_{svc.id}"):
                continue
            picked_any = True
            mode = _g_str(form, f"z_kit_mode_{svc.id}", "STOCK").upper()
            det: dict[str, Any] = {}
            if mode == "OFF":
                qty = _g_int(form, f"z_off_qty_{svc.id}", 0)
                desc = _g_str(form, f"z_off_desc_{svc.id}")
                if qty <= 0:
                    raise ValueError(f"Для «Вне учёта» укажите количество (услуга: {svc.name}).")
                det["off_book"] = {"quantity": qty, "description": desc or None}
            elif mode == "STOCK":
                sk, ue, bu = _parse_kit_stock_from_form(form, f"z_{svc.id}_")
                if not sk:
                    raise ValueError(f"Выберите комплект из наличия ({svc.name}).")
                det["kit"] = _kit_block_stock_json(db, sk, ue, bu)
                n, cost, sf = _apply_stock_kit_usage(db, kit_id=sk, use_entire=ue, blanks_used=bu)
                kit_usages.append((sk, n, cost))
                kit_cost_total += cost
                kit_studio_fund += sf
            elif mode == "NEW":
                prefix = f"z_new_{svc.id}_"
                kit = _create_kit_from_prefixed_form(db, form, prefix)
                n, cost, sf = _apply_stock_kit_usage(db, kit_id=kit.id, use_entire=True, blanks_used=0)
                det["kit"] = {
                    "kind": "STOCK",
                    "from_stock": {
                        "sku": kit.sku,
                        "blanks_used": n,
                        "use_entire_kit": True,
                    },
                    "new_kit": None,
                    "own": None,
                }
                kit_usages.append((kit.id, n, cost))
                kit_cost_total += cost
                kit_studio_fund += sf
            else:
                raise ValueError(f"Выберите тип комплекта для услуги «{svc.name}».")
            service_lines.append((svc, sort_i, det))
            sort_i += 1
        if not picked_any:
            raise ValueError("Отметьте хотя бы одну услугу (заготовки поштучно).")

    elif sub_key == StudioOrderSubcategoryKey.REZINKI:
        tail_id, braid_id = _rubber_tail_braid_ids(db)
        want_tail = _g_bool(form, "rubber_tail")
        want_braid = _g_bool(form, "rubber_braid")
        other_id = _g_int(form, "rubber_other_service_id", 0)
        rubber_length = _g_float(form, "rubber_length_cm", 0) or None
        rubber_blanks = _g_int(form, "rubber_blanks_on_elastic", 0) or None
        rubber_weight = _g_float(form, "rubber_weight_grams", 0) or None
        rubber_desc = _g_str(form, "rubber_description") or None
        if rubber_length is None or rubber_length <= 0:
            raise ValueError("Укажите длину (см).")
        if rubber_blanks is None or rubber_blanks <= 0:
            raise ValueError("Укажите количество заготовок на резинке (целое число).")
        if rubber_weight is None or rubber_weight <= 0:
            raise ValueError("Укажите вес.")
        ids_line: list[int] = []
        if want_tail and tail_id:
            ids_line.append(tail_id)
        if want_braid and braid_id:
            ids_line.append(braid_id)
        ids_set: set[int] = set(ids_line)
        if other_id > 0:
            s_other = _service_in_sub(db, other_id, SUB_REZINKI)
            ids_set.add(s_other.id)
        ids_line = sorted(ids_set)
        if not ids_line:
            raise ValueError("Выберите хотя бы одну услугу резинок (галочки или позиция из списка).")
        extra_time = want_tail or want_braid
        if extra_time:
            duration_minutes = _g_int(form, "rubber_duration_h", 0) * 60 + _g_int(
                form, "rubber_duration_m", 0
            )
            ar = _g_str(form, "rubber_amortization_level", "") or "MIN"
            try:
                amort_level = AmortizationLevel(ar)
            except ValueError:
                amort_level = AmortizationLevel.MIN
            amort_amount = {"MIN": 100.0, "MID": 200.0, "MAX": 500.0}[amort_level.value]
        for sid in ids_line:
            svc = db.get(Service, sid)
            if not svc:
                continue
            service_lines.append((svc, sort_i, {"rubber_common": True}))
            sort_i += 1

    elif sub_key == StudioOrderSubcategoryKey.KORREKTSIYA:
        k_sub = next((x for x in load_zakaz_catalog(db) if x["name"] == SUB_KORREKTSIYA), None)
        if not k_sub:
            raise ValueError("В каталоге нет подкатегории «Коррекция комплекта».")
        picked_any = False
        for svc in k_sub["services"]:
            if not _g_bool(form, f"k_on_{svc.id}"):
                continue
            picked_any = True
            service_lines.append((svc, sort_i, {}))
            sort_i += 1
        if not picked_any:
            raise ValueError("Отметьте хотя бы одну услугу коррекции.")
        kor_blanks = _g_int(form, "korrekciya_blanks_in_kit", 0)
        if kor_blanks <= 0:
            raise ValueError("Укажите количество заготовок в комплекте (целое число > 0).")
        kor_comment = _g_str(form, "korrekciya_comment") or None

    cost_total = mat_cost + kit_cost_total + addons + mix_cost + amort_amount
    profit_before = common["amount_from_client"] - cost_total
    salon_profit = profit_before * salon_pct
    masters_pool = profit_before - salon_profit

    order = StudioOrder(
        created_by_user_id=created_by_user_id,
        performed_date=performed_dt,
        duration_minutes=duration_minutes,
        client_id=client.id,
        client_type=common["client_type"],
        price_type=common["price_type"],
        client_age_group=client.age_group,
        kanekalon_grams=common["kanekalon_grams"],
        kudri_grams=common["kudri_grams"],
        mix_source=common["mix_source"],
        mix_complexity=common["mix_complexity"],
        mix_cost_amount=mix_cost,
        mix_bonus_master_id=mix_bonus_master_id,
        mix_bonus_amount=mix_bonus_amount,
        kanekalon_price_per_gram_at_time=k_snap,
        kudri_price_per_gram_at_time=ku_snap,
        materials_cost_total=mat_cost,
        amount_from_client=common["amount_from_client"],
        comment=common["order_comment"] or None,
        addons_total=addons,
        addons_details_json=addons_details_json,
        amortization_level=amort_level,
        amortization_amount=amort_amount,
        studio_fund_amount=amort_amount + kit_studio_fund,
        cost_total=cost_total,
        profit_before_split=profit_before,
        salon_cut_pct_at_time=salon_pct,
        salon_profit=salon_profit,
        masters_pool=masters_pool,
        subcategory_key=sub_key,
        rubber_length_cm=rubber_length,
        rubber_blanks_on_elastic=rubber_blanks,
        rubber_weight_grams=rubber_weight,
        rubber_description=rubber_desc,
        korrekciya_blanks_in_kit=kor_blanks,
        korrekciya_comment=kor_comment,
    )
    db.add(order)
    db.flush()

    for uid, pct in staff_alloc:
        db.add(StudioOrderStaff(studio_order_id=order.id, user_id=uid, percent=pct))

    for svc, so, det in service_lines:
        db.add(
            StudioOrderServiceLine(
                studio_order_id=order.id,
                service_id=svc.id,
                sort_order=so,
                details_json=json.dumps(det, ensure_ascii=False) if det else None,
            )
        )

    for kid, pieces, camount in kit_usages:
        db.add(
            StudioOrderKitUsage(
                studio_order_id=order.id,
                kit_id=kid,
                pieces_used=pieces,
                cost_amount=camount,
                note=None,
            )
        )

    db.commit()
    db.refresh(order)
    return order


templates = Jinja2Templates(directory="app/templates")
router = APIRouter(prefix="/sales/order", tags=["studio-order"])
_STAFF = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER))


def _ctx(request: Request, current_user: AuthUser, **kwargs):
    return {"request": request, "current_user": current_user, **kwargs}


def _new_order_template_kwargs(db: Session):
    zc = load_zakaz_catalog(db)
    staff = list_staff_for_order_form(db)
    tail_id, braid_id = _rubber_tail_braid_ids(db)
    others: list[Service] = []
    sub = next((x for x in zc if x["name"] == SUB_REZINKI), None)
    if sub:
        for svc in sub["services"]:
            if svc.id == tail_id or svc.id == braid_id:
                continue
            others.append(svc)
    return {
        "zakaz_catalog": zc,
        "staff_for_order": staff,
        "staff_for_kit_authors": list_masters_for_kit_author_pick(db),
        "rubber_tail_id": tail_id,
        "rubber_braid_id": braid_id,
        "rubber_others": others,
        "default_date": date.today().isoformat(),
    }


@router.get("/new", response_class=HTMLResponse)
def studio_order_new_get(
    request: Request,
    current_user: AuthUser = _STAFF,
    db: Session = Depends(get_db),
):
    kw = _new_order_template_kwargs(db)
    return templates.TemplateResponse(
        "studio_order_new.html",
        _ctx(request, current_user=current_user, error=None, **kw),
    )


@router.post("/new")
async def studio_order_new_post(
    request: Request,
    current_user: AuthUser = _STAFF,
    db: Session = Depends(get_db),
):
    form = await request.form()
    try:
        save_studio_order_from_form(
            db,
            form=form,
            created_by_user_id=current_user.id,
            created_by_label=format_created_by_label(current_user),
        )
    except ValueError as exc:
        kw = _new_order_template_kwargs(db)
        return templates.TemplateResponse(
            "studio_order_new.html",
            _ctx(request, current_user=current_user, error=str(exc), **kw),
            status_code=400,
        )
    return RedirectResponse(url="/sales/order?msg=saved", status_code=303)


@router.get("", response_class=HTMLResponse)
def studio_order_list(
    request: Request,
    current_user: AuthUser = _STAFF,
    db: Session = Depends(get_db),
):
    msg = request.query_params.get("msg")
    rows = list(
        db.scalars(
            select(StudioOrder)
            .options(
                selectinload(StudioOrder.client),
                selectinload(StudioOrder.staff_rows).selectinload(StudioOrderStaff.user),
                selectinload(StudioOrder.service_lines).selectinload(StudioOrderServiceLine.service),
            )
            .order_by(StudioOrder.id.desc())
            .limit(100)
        ).all()
    )
    return templates.TemplateResponse(
        "studio_orders_list.html",
        _ctx(request, current_user=current_user, rows=rows, msg=msg),
    )
