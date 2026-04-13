"""
Работа с товарами: единая форма (в наличие / на заказ) + запись в work_for_inventory.
Этап 6.3.2: каркас, без детальных расчётов по видам.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.payroll_fund import post_work_accruals, replace_work_accruals, storno_source_accruals
from starlette.datastructures import UploadFile

from app.auth import AuthUser, require_role
from app.client_validation import format_created_by_label
from app.db.models import (
    Client,
    Kit,
    KitAuthorStaff,
    PayrollFundSourceKind,
    ProductSale,
    MaterialPriceCurrent,
    MaterialType,
    MixComplexity,
    MixSource,
    User,
    UserRole,
    Visit,
    VisitKitUsage,
    WorkForInventoryAuditLog,
    WorkForInventory,
    WorkForInventoryStaff,
    WorkKind,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    WorkRate,
    WorkScope,
)
from app.db.session import get_db
from app.user_roles import select_users_with_role, user_has_role
from app.kit_inlay_visit import _materials_cost_and_snapshot
from app.work_products_compute import compute_work_financials
from app.visit_edit_policy import edit_window_days, is_in_closed_payroll_period, within_edit_window
from app.ru_labels import ru_user_role
from app.audit import diff_fields, write_audit_rows

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["ru_user_role"] = ru_user_role
router = APIRouter(prefix="/sales/work", tags=["work-products"])
_VIEW = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER))
_MASTER = Depends(require_role(UserRole.MASTER))
_SUPER = Depends(require_role(UserRole.ADMIN_SUPER))


def _ctx(request: Request, current_user: AuthUser, **kwargs):
    return {"request": request, "current_user": current_user, **kwargs}


def _work_edit_allowed(db: Session, work: WorkForInventory) -> tuple[bool, str]:
    if getattr(work, "is_voided", False):
        return False, "Работа аннулирована — редактирование запрещено."
    if is_in_closed_payroll_period(db, work.created_at):
        return False, "Работа относится к закрытому периоду ЗП — редактирование запрещено."
    days = edit_window_days(db)
    if not within_edit_window(work, days):
        return False, (
            f"Редактирование доступно только в течение {days} дн. с даты создания "
            "(параметр «Окно редактирования» в настройках студии)."
        )
    return True, ""


def _g_str(form: Any, name: str, default: str = "") -> str:
    v = form.get(name)
    if v is None or isinstance(v, UploadFile):
        return default
    if isinstance(v, (bytes, bytearray)):
        return v.decode().strip()
    return str(v).strip()


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


def _list_masters_for_work_form(db: Session) -> list[User]:
    return list(
        db.scalars(
            select_users_with_role(UserRole.MASTER).order_by(
                User.display_name.asc(), User.id.asc()
            )
        ).all()
    )


def _read_kit_master_on_ids(form: Any) -> list[int]:
    raw: list[Any] = []
    if hasattr(form, "getlist"):
        raw = list(form.getlist("kit_master_on"))
    else:
        v = form.get("kit_master_on")
        if v is not None:
            raw = [v]

    seen: set[int] = set()
    out: list[int] = []
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
        out.append(i)
    return out


def _kit_qty_prefill_from_form(form: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for k in form.keys():
        if not isinstance(k, str) or not k.startswith("kit_qty_"):
            continue
        out[k] = _g_str(form, k, "0")
    return out


# Стрижки в ЗП, но не учитываются в «количестве заготовок» для склада / предпросмотра.
KIT_INVENTORY_PIECE_EXCLUDE_KEYS = frozenset({"SE_TRIM_SHORT", "SE_TRIM_LONG", "DE_TRIM"})


def _material_prices_per_gram(db: Session) -> tuple[float, float]:
    pk = db.get(MaterialPriceCurrent, MaterialType.KANEKALON)
    pku = db.get(MaterialPriceCurrent, MaterialType.KUDRI)
    return (
        float(pk.price_per_gram) if pk else 0.0,
        float(pku.price_per_gram) if pku else 0.0,
    )


def _kit_table_state_json(
    current_user: AuthUser,
    masters: list[User],
    kit_qty_prefill: dict[str, str],
    db: Session,
) -> str:
    kpg, kudpg = _material_prices_per_gram(db)
    return json.dumps(
        {
            "currentUserId": current_user.id,
            "masters": [{"id": u.id, "name": u.display_name} for u in masters],
            "seItems": [{"key": k, "label": lbl} for k, lbl in _kit_se_items()],
            "deItems": [{"key": k, "label": lbl} for k, lbl in _kit_de_items()],
            "prefill": kit_qty_prefill,
            "kitRates": _kit_rates_defaults(),
            "excludeFromInventoryPieceCount": sorted(KIT_INVENTORY_PIECE_EXCLUDE_KEYS),
            "materialPricePerGram": {"kanekalon": kpg, "kudri": kudpg},
        },
        ensure_ascii=False,
    )


def _alloc_equal_shares_for_masters(db: Session, user_ids: list[int]) -> list[tuple[int, float]]:
    """Доли 1/n для строк work_for_inventory_staff; сумма ≈ 1.00. ЗП по заготовкам — в master_profit_amount."""
    if not user_ids:
        raise ValueError("Нет мастеров для записи работы.")
    seen: set[int] = set()
    ordered: list[int] = []
    for uid in user_ids:
        if uid in seen:
            continue
        seen.add(uid)
        ordered.append(uid)
        u = db.get(User, uid)
        if not u or not u.is_active:
            raise ValueError(f"Мастер (ID {uid}) не найден или отключён.")
        if not user_has_role(db, uid, UserRole.MASTER):
            raise ValueError("В комплекте участвуют только мастера.")
    n = len(ordered)
    if n == 1:
        return [(ordered[0], 1.0)]
    base = round(1.0 / n, 2)
    shares: list[float] = []
    acc = 0.0
    for _ in range(n - 1):
        shares.append(base)
        acc += base
    last = round(1.0 - acc, 2)
    if last < 0:
        last = 0.0
    shares.append(last)
    return [(ordered[i], shares[i]) for i in range(n)]


def _studio_share_snapshot(db: Session) -> float:
    r = db.scalar(select(WorkRate).where(WorkRate.key == "studio_share", WorkRate.is_active.is_(True)))
    if not r:
        return 0.30
    try:
        return float(json.loads(r.value_json))
    except Exception:
        return 0.30


def _wr_float(db: Session, key: str, default: float) -> float:
    r = db.scalar(select(WorkRate).where(WorkRate.key == key, WorkRate.is_active.is_(True)))
    if not r:
        return default
    try:
        return float(json.loads(r.value_json))
    except Exception:
        return default


def _rubber_type_items() -> list[tuple[str, str]]:
    return [
        ("TAIL_ELASTIC", "Хвост на резинке"),
        ("TAIL_CRAB", "Хвост на крабе"),
        ("TAIL_NET", "Хвост на сетке"),
        ("BRAIDS_ELASTIC", "Косы на резинке"),
    ]


def _rubber_service_name(rubber_type: str) -> str:
    return {
        "TAIL_ELASTIC": "Хвост на резинке (1 крепление)",
        "TAIL_CRAB": "Хвост на крабе",
        "TAIL_NET": "Хвост на сетке",
        "BRAIDS_ELASTIC": "Косы на резинке (1 коса)",
    }[rubber_type]


def _rubber_pricing_from_catalog(db: Session, rubber_type: str) -> tuple[float, float, float, bool, str | None]:
    """
    Возвращает: (master_pay, studio_pay, fixed_expense, is_per_unit, unit_label).
    Берём из каталога услуг: категория «Заказ» → подкатегория «Хвосты/резинки».
    """
    cat = db.scalar(select(ServiceCategory).where(ServiceCategory.name == "Заказ"))
    if not cat:
        raise ValueError("Не найден прайс: категория «Заказ».")
    sub = db.scalar(
        select(ServiceSubcategory).where(ServiceSubcategory.category_id == cat.id, ServiceSubcategory.name == "Хвосты/резинки")
    )
    if not sub:
        raise ValueError("Не найден прайс: «Заказ → Хвосты/резинки».")
    svc_name = _rubber_service_name(rubber_type)
    svc = db.scalar(select(Service).where(Service.subcategory_id == sub.id, Service.name == svc_name))
    # Подкатегория может быть скрыта из прайса «Товары» (is_active=false), но строки услуг
    # остаются для внутреннего расчёта ЗП/фонда в «Работа с товарами».
    if not svc:
        raise ValueError(f"Не найден прайс для «{svc_name}».")
    mp = float(svc.master_pay_amount or 0.0)
    sp = float(svc.studio_pay_amount or 0.0)
    fx = float(svc.fixed_expense_amount or 0.0)
    return mp, sp, fx, bool(svc.is_per_unit), (svc.unit_label or None)


def _zakaz_subcategory_services_map(
    db: Session, subcategory_name: str
) -> dict[str, dict[str, float | bool | None]]:
    """
    Возвращает map по имени услуги в подкатегории:
    { name: {client_from, client_to, master_pay, studio_pay, fixed_expense, is_per_unit} }
    """
    cat = db.scalar(select(ServiceCategory).where(ServiceCategory.name == "Заказ"))
    if not cat:
        return {}
    sub = db.scalar(
        select(ServiceSubcategory).where(
            ServiceSubcategory.category_id == cat.id, ServiceSubcategory.name == subcategory_name
        )
    )
    if not sub:
        return {}
    rows = list(db.scalars(select(Service).where(Service.subcategory_id == sub.id, Service.is_active.is_(True))).all())
    out: dict[str, dict[str, float | bool | None]] = {}
    for s in rows:
        out[s.name] = {
            "client_from": float(s.price_middle_from) if s.price_middle_from is not None else None,
            "client_to": float(s.price_middle_to) if s.price_middle_to is not None else None,
            "master_pay": float(s.master_pay_amount) if s.master_pay_amount is not None else None,
            "studio_pay": float(s.studio_pay_amount) if s.studio_pay_amount is not None else None,
            "fixed_expense": float(s.fixed_expense_amount) if s.fixed_expense_amount is not None else None,
            "is_per_unit": bool(s.is_per_unit),
        }
    return out


def _details_obj(details_json: str | None) -> dict[str, Any]:
    if not details_json:
        return {}
    try:
        v = json.loads(details_json)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _ru_mix_source(v: MixSource | None) -> str:
    if v == MixSource.NO_MIX:
        return "Без смешки"
    if v == MixSource.FROM_STOCK:
        return "Из наличия"
    if v == MixSource.SELF_MIXED:
        return "Сама мешала"
    return "—"


def _ru_mix_complexity(v: str | None) -> str:
    return {"SIMPLE": "Простая", "MEDIUM": "Средняя", "HARD": "Сложная"}.get(v or "", "—")


def _kind_label(k: WorkKind) -> str:
    return {
        WorkKind.KIT: "Комплект/Заготовки (поштучно)",
        WorkKind.MIX: "Смешка",
        WorkKind.RUBBER: "Хвосты/резинки",
        WorkKind.KIT_CORRECTION: "Коррекция комплекта",
        WorkKind.HAIR_EXT_PREP: "Подготовка к наращиванию волос (заглушка)",
    }[k]


def _kit_se_items() -> list[tuple[str, str]]:
    return [
        ("SE_BRAID_SHORT", "SE: коса короткая"),
        ("SE_BRAID_LONG", "SE: коса длинная"),
        ("SE_BRAID_FREE_TIP", "SE: коса свободный кончик"),
        ("SE_TIP_ADDON", "SE: доплёт кончиков"),
        ("SE_TRIM_SHORT", "SE: стрижка короткой косы"),
        ("SE_TRIM_LONG", "SE: стрижка длинной косы"),
    ]


def _kit_de_items() -> list[tuple[str, str]]:
    return [
        ("DE_BRAID_SHORT", "DE: коса короткая"),
        ("DE_BRAID_LONG", "DE: коса длинная"),
        ("DE_BRAID_NEW_FMT", "DE: коса новый формат"),
        ("DE_CURL", "DE: кудря"),
        ("DE_DREAD_FREE_TIP", "DE: дред свободный кончик"),
        ("DE_DREAD_SHORT", "DE: дред короткий"),
        ("DE_DREAD_LONG", "DE: дред длинный"),
        ("DE_TRIM", "DE: стрижка"),
    ]


def _kit_rates_defaults() -> dict[str, dict[str, Any]]:
    """
    Ставки ЗП за 1 шт по видам (по умолчанию). Пороговые цены — только для конкретного вида.
    Значения можно переопределить позже через work_rates (следующий шаг расширит настройки).
    """
    return {
        "SE": {
            "SE_BRAID_SHORT": {"base": 12.5, "threshold_qty": 140, "threshold_rate": 11.0},
            "SE_BRAID_LONG": {"base": 15.0, "threshold_qty": 120, "threshold_rate": 13.5},
            "SE_BRAID_FREE_TIP": {"base": 11.0},
            "SE_TIP_ADDON": {"base": 5.0},
            "SE_TRIM_SHORT": {"base": 2.0},
            "SE_TRIM_LONG": {"base": 2.5},
        },
        "DE": {
            "DE_BRAID_SHORT": {"base": 25.0},
            "DE_BRAID_LONG": {"base": 30.0},
            "DE_BRAID_NEW_FMT": {"base": 35.0},
            "DE_CURL": {"base": 25.0},
            "DE_DREAD_FREE_TIP": {"base": 35.0},
            "DE_DREAD_SHORT": {"base": 40.0},
            "DE_DREAD_LONG": {"base": 50.0},
            "DE_TRIM": {"base": 5.0},
        },
    }


def _kit_rate_for_item(rates: dict[str, dict[str, Any]], item_key: str, qty_total: int) -> float:
    for group in ("SE", "DE"):
        if item_key in rates.get(group, {}):
            cfg = rates[group][item_key]
            base = float(cfg.get("base") or 0.0)
            th_qty = int(cfg.get("threshold_qty") or 0)
            th_rate = float(cfg.get("threshold_rate") or 0.0)
            if th_qty > 0 and qty_total >= th_qty and th_rate > 0:
                return th_rate
            return base
    return 0.0


@router.get("/new", response_class=HTMLResponse)
def work_new_get(
    request: Request,
    current_user: AuthUser = _MASTER,
    db: Session = Depends(get_db),
):
    masters = _list_masters_for_work_form(db)
    work_price_meta = {
        "rubber": _zakaz_subcategory_services_map(db, "Хвосты/резинки"),
        "correction": _zakaz_subcategory_services_map(db, "Коррекция комплекта"),
        "customOrderBonus": _wr_float(db, "custom_order_bonus_multiplier", 1.0),
        "mixRates": {
            "SIMPLE": _wr_float(db, "mix_simple", 1.0),
            "MEDIUM": _wr_float(db, "mix_medium", 1.5),
            "HARD": _wr_float(db, "mix_hard", 2.0),
        },
    }
    return templates.TemplateResponse(
        "work_products_new.html",
        _ctx(
            request,
            current_user=current_user,
            error=None,
            fp={},
            masters=masters,
            kit_master_on_ids=[],
            kit_table_state_json=_kit_table_state_json(current_user, masters, {}, db),
            default_date=date.today().isoformat(),
            kinds=[{"value": k.value, "label": _kind_label(k)} for k in WorkKind],
            scopes=[{"value": s.value, "label": ("В наличие" if s == WorkScope.IN_STOCK else "На заказ")} for s in WorkScope],
            kit_se_items=_kit_se_items(),
            kit_de_items=_kit_de_items(),
            rubber_types=[{"value": v, "label": l} for v, l in _rubber_type_items()],
            work_price_meta_json=json.dumps(work_price_meta, ensure_ascii=False),
        ),
    )


@router.post("/new")
async def work_new_post(
    request: Request,
    current_user: AuthUser = _MASTER,
    db: Session = Depends(get_db),
):
    form = await request.form()
    fp = {k: _g_str(form, k) for k in form.keys() if isinstance(k, str)}
    try:
        scope_raw = (_g_str(form, "scope", "") or "").strip()
        try:
            scope = WorkScope(scope_raw)
        except ValueError:
            raise ValueError("Выберите режим: в наличие или на заказ.")

        kind_raw = (_g_str(form, "kind", "") or "").strip()
        try:
            kind = WorkKind(kind_raw)
        except ValueError:
            raise ValueError("Выберите вид работы.")

        client_id: int | None = None
        amount_from_client: int | None = None
        if scope == WorkScope.CUSTOM_ORDER:
            cid_raw = (_g_str(form, "client_id", "") or "").strip()
            if not cid_raw.isdigit():
                raise ValueError("Для режима «на заказ» выберите клиента.")
            client_id = int(cid_raw)
            if not db.get(Client, client_id):
                raise ValueError("Клиент не найден.")
            afc_raw = (_g_str(form, "amount_from_client", "") or "").strip()
            if afc_raw:
                try:
                    amount_from_client = int(float(afc_raw.replace(",", ".")))
                except ValueError:
                    raise ValueError("Сумма от клиента должна быть числом.")
                if amount_from_client < 0:
                    raise ValueError("Сумма от клиента не может быть отрицательной.")

        kanek = max(0.0, _g_float(form, "kanekalon_grams", 0.0))
        kudri = max(0.0, _g_float(form, "kudri_grams", 0.0))
        grams_total = kanek + kudri
        mix_raw = _g_str(form, "mix_source", "")
        if grams_total <= 0:
            mix_source = MixSource.NO_MIX
        else:
            try:
                mix_source = MixSource(mix_raw) if mix_raw else MixSource.NO_MIX
            except ValueError:
                mix_source = MixSource.NO_MIX
        if kind == WorkKind.MIX:
            # For "Смешка" work type the mix is always self-mixed (UI hides the selector).
            mix_source = MixSource.SELF_MIXED if grams_total > 0 else MixSource.NO_MIX

        mix_complexity: MixComplexity | None = None
        if grams_total > 0 and mix_source != MixSource.NO_MIX:
            mc_raw = _g_str(form, "mix_complexity", "")
            try:
                mix_complexity = MixComplexity(mc_raw) if mc_raw else None
            except ValueError:
                mix_complexity = None
        if kind == WorkKind.MIX and grams_total <= 0:
            raise ValueError("Для вида «Смешка» укажите граммы материала.")
        if kind == WorkKind.MIX and mix_complexity is None:
            raise ValueError("Для вида «Смешка» выберите сложность.")

        # snapshots for materials
        mat_cost, k_snap, ku_snap = _materials_cost_and_snapshot(
            db, kanekalon_grams=kanek, kudri_grams=kudri
        )

        details: dict[str, Any] = {}
        if mix_complexity is not None:
            details["mix_complexity"] = mix_complexity.value

        # For KIT extra costs are manually entered; for RUBBER they are computed from catalog.
        extra_costs_amount = max(0.0, _g_float(form, "extra_costs_amount", 0.0))

        alloc: list[tuple[int, float]]
        kit_staff_ids: list[int] = []
        rubber_type = ""
        rubber_qty = 1
        corr_trim_qty = 0
        corr_wash = False
        corr_circle = False
        corr_steam = False
        corr_dread_qty = 0
        corr_curl_qty = 0
        corr_curl_dread_complexity: str | None = None
        # KIT: parse blanks + compute master/studio profit
        kit_rates = _kit_rates_defaults()
        kit_totals: dict[str, int] = {}
        kit_by_staff: dict[int, dict[str, int]] = {}
        kit_pieces_total = 0
        kit_blank_type_se = _g_bool(form, "kit_type_se")
        kit_blank_type_de = _g_bool(form, "kit_type_de")
        kit_use_multi_masters = _g_bool(form, "kit_use_multi_masters")
        if kind == WorkKind.KIT:
            if not kit_blank_type_se and not kit_blank_type_de:
                raise ValueError("Для комплекта выберите тип заготовок: SE и/или DE.")
            if kit_use_multi_masters:
                kit_staff_ids = _read_kit_master_on_ids(form)
                if not kit_staff_ids:
                    raise ValueError(
                        "Для таблицы комплекта отметьте мастеров или снимите «Несколько мастеров (комплект)»."
                    )
            else:
                kit_staff_ids = [current_user.id]
                cu = db.get(User, current_user.id)
                if not cu or not user_has_role(db, current_user.id, UserRole.MASTER):
                    raise ValueError(
                        "Режим одной колонки доступен только при входе под мастером; "
                        "иначе отметьте «Несколько мастеров (комплект)» и выберите мастеров."
                    )
            all_items = []
            if kit_blank_type_se:
                all_items.extend(_kit_se_items())
            if kit_blank_type_de:
                all_items.extend(_kit_de_items())
            any_qty = False
            for uid in kit_staff_ids:
                per: dict[str, int] = {}
                for item_key, _ in all_items:
                    raw = _g_str(form, f"kit_qty_{uid}_{item_key}", "0")
                    try:
                        q = int(raw or "0")
                    except ValueError:
                        q = 0
                    q = max(0, q)
                    if q > 0:
                        any_qty = True
                    per[item_key] = q
                    kit_totals[item_key] = kit_totals.get(item_key, 0) + q
                kit_by_staff[uid] = per
            if not any_qty:
                raise ValueError("Для комплекта укажите хотя бы одно количество заготовок.")
            kit_pieces_total = sum(kit_totals.values())
            kit_pieces_inventory = sum(
                q for k, q in kit_totals.items() if k not in KIT_INVENTORY_PIECE_EXCLUDE_KEYS
            )
            details["kit"] = {
                "blank_type_se": kit_blank_type_se,
                "blank_type_de": kit_blank_type_de,
                "totals": kit_totals,
                "by_staff": kit_by_staff,
            }
            alloc = _alloc_equal_shares_for_masters(db, kit_staff_ids)
        elif kind == WorkKind.RUBBER:
            rubber_type = (_g_str(form, "rubber_type", "") or "").strip()
            if rubber_type not in {k for k, _ in _rubber_type_items()}:
                raise ValueError("Для «Хвосты/резинки» выберите тип.")

            if rubber_type == "TAIL_ELASTIC":
                rubber_qty = int(_g_float(form, "rubber_attach_qty", 0))
                if rubber_qty <= 0:
                    raise ValueError("Укажите количество креплений для хвоста на резинке (целое число).")
            elif rubber_type == "BRAIDS_ELASTIC":
                rubber_qty = int(_g_float(form, "rubber_braids_qty", 0))
                if rubber_qty <= 0:
                    raise ValueError("Укажите количество кос для кос на резинке (целое число).")
            else:
                rubber_qty = 1

            details["rubber"] = {
                "type": rubber_type,
                "qty": rubber_qty,
            }
            alloc = [(current_user.id, 1.0)]
        elif kind == WorkKind.KIT_CORRECTION:
            corr_trim_qty = int(_g_float(form, "corr_trim_qty", 0))
            corr_wash = _g_bool(form, "corr_wash")
            corr_circle = _g_bool(form, "corr_circle")
            corr_steam = _g_bool(form, "corr_steam")
            corr_dread_qty = int(_g_float(form, "corr_dread_qty", 0))
            corr_curl_qty = int(_g_float(form, "corr_curl_qty", 0))
            corr_cd_raw = (_g_str(form, "corr_curl_dread_complexity", "") or "").strip()
            if corr_dread_qty <= 0 and corr_curl_qty <= 0:
                corr_curl_dread_complexity = None
            else:
                if corr_cd_raw not in ("NORMAL", "HARD"):
                    corr_cd_raw = "NORMAL"
                corr_curl_dread_complexity = corr_cd_raw

            if corr_wash and corr_circle:
                raise ValueError("Если выбрана «Стирка», то «Одевание на круг» выбирать нельзя (входит в стирку).")

            if any(x < 0 for x in (corr_trim_qty, corr_dread_qty, corr_curl_qty)):
                raise ValueError("Количество должно быть неотрицательным числом.")

            if (
                (corr_trim_qty <= 0)
                and (not corr_wash)
                and (not corr_circle)
                and (not corr_steam)
                and (corr_dread_qty <= 0)
                and (corr_curl_qty <= 0)
            ):
                raise ValueError("Для «Коррекция комплекта» выберите хотя бы один пункт.")

            details["correction"] = {
                "trim_qty": corr_trim_qty,
                "wash": corr_wash,
                "circle": corr_circle,
                "steam": corr_steam,
                "dread_qty": corr_dread_qty,
                "curl_qty": corr_curl_qty,
                "curl_dread_complexity": corr_curl_dread_complexity,
            }
            alloc = [(current_user.id, 1.0)]
        else:
            alloc = [(current_user.id, 1.0)]

        ready_date_raw = _g_str(form, "ready_date", "")
        ready_dt = None
        if ready_date_raw:
            try:
                ready_dt = datetime.combine(date.fromisoformat(ready_date_raw), datetime.min.time())
            except ValueError:
                raise ValueError("Некорректная дата готовности (ожидается YYYY-MM-DD).")

        fin = compute_work_financials(
            db,
            kind=kind,
            scope=scope,
            alloc=alloc,
            current_user_id=current_user.id,
            mat_cost=mat_cost,
            kit_totals=kit_totals,
            kit_rates=kit_rates,
            kit_staff_ids=kit_staff_ids,
            kit_by_staff=kit_by_staff,
            mix_source=mix_source,
            mix_complexity=mix_complexity,
            grams_total=grams_total,
            rubber_type=rubber_type,
            rubber_qty=rubber_qty,
            corr_trim_qty=corr_trim_qty,
            corr_wash=corr_wash,
            corr_circle=corr_circle,
            corr_steam=corr_steam,
            corr_dread_qty=corr_dread_qty,
            corr_curl_qty=corr_curl_qty,
            corr_curl_dread_complexity=corr_curl_dread_complexity,
        )
        staff_master_profit = fin.staff_master_profit
        master_total = fin.master_total
        studio_total = fin.studio_total
        profit_total = fin.profit_total
        extra_costs_amount = fin.extra_costs_amount
        cost_total_amount = fin.cost_total_amount
        studio_share = fin.studio_share_snapshot

        work = WorkForInventory(
            created_by_user_id=current_user.id,
            kind=kind,
            scope=scope,
            client_id=client_id,
            amount_from_client=amount_from_client,
            ready_date=ready_dt,
            comment=(_g_str(form, "comment", "") or "").strip() or None,
            kanekalon_grams=kanek,
            kudri_grams=kudri,
            mix_source=mix_source,
            kanekalon_price_per_gram_at_time=k_snap,
            kudri_price_per_gram_at_time=ku_snap,
            materials_cost_total=mat_cost,
            studio_share_snapshot=studio_share,
            rates_snapshot_json=None,
            details_json=json.dumps(details, ensure_ascii=False) if details else None,
            extra_costs_amount=extra_costs_amount,
            cost_total_amount=cost_total_amount,
            master_profit_amount=master_total,
            studio_profit_amount=studio_total,
            profit_total_amount=profit_total,
        )
        db.add(work)
        db.flush()

        for uid, share in alloc:
            db.add(
                WorkForInventoryStaff(
                    work_id=work.id,
                    user_id=uid,
                    share=share,
                    master_profit_amount=float(staff_master_profit.get(uid, 0.0)),
                    details_json=None,
                )
            )

        # Create Kit in stock for IN_STOCK + KIT
        if kind == WorkKind.KIT and scope == WorkScope.IN_STOCK:
            sku = (_g_str(form, "kit_sku", "") or "").strip()
            title = (_g_str(form, "kit_title", "") or "").strip()
            if not sku:
                raise ValueError("Для «в наличие» укажите артикул комплекта.")
            if not title:
                raise ValueError("Для «в наличие» укажите название комплекта.")
            if db.scalar(select(Kit.id).where(Kit.sku == sku)):
                raise ValueError("Комплект с таким артикулом уже есть — укажите другой.")
            full_cost = float(cost_total_amount) + float(master_total)
            kit = Kit(
                sku=sku[:80],
                title=title[:200],
                description=None,
                is_active=True,
                pieces_total=kit_pieces_inventory,
                pieces_available=kit_pieces_inventory,
                blank_type_se=kit_blank_type_se,
                blank_type_de=kit_blank_type_de,
                weight_grams=None,
                length_cm=None,
                has_decorations=False,
                materials_text=None,
                color_text=None,
                blanks_kinds_text=None,
                notes=None,
                stock_price_total=None,
                discount_percent=0,
                cost_total=full_cost,
                author_cost_total=None,
                created_at=datetime.utcnow(),
                is_in_stock=True,
                is_archived=False,
            )
            db.add(kit)
            db.flush()
            work.created_kit_id = kit.id
            seen_uid: set[int] = set()
            so = 0
            for uid in kit_staff_ids:
                if uid <= 0 or uid in seen_uid:
                    continue
                seen_uid.add(uid)
                mu = db.get(User, uid)
                if mu and mu.is_active and user_has_role(db, uid, UserRole.MASTER):
                    db.add(KitAuthorStaff(kit_id=kit.id, user_id=uid, sort_order=so))
                    so += 1

        staff_saved = list(
            db.scalars(
                select(WorkForInventoryStaff).where(WorkForInventoryStaff.work_id == work.id)
            ).all()
        )
        post_work_accruals(db, work.id, staff_saved, current_user.id)
        db.commit()
        return RedirectResponse(url="/sales/work?msg=saved", status_code=303)
    except ValueError as exc:
        masters = _list_masters_for_work_form(db)
        work_price_meta = {
            "rubber": _zakaz_subcategory_services_map(db, "Хвосты/резинки"),
            "correction": _zakaz_subcategory_services_map(db, "Коррекция комплекта"),
            "customOrderBonus": _wr_float(db, "custom_order_bonus_multiplier", 1.0),
            "mixRates": {
                "SIMPLE": _wr_float(db, "mix_simple", 1.0),
                "MEDIUM": _wr_float(db, "mix_medium", 1.5),
                "HARD": _wr_float(db, "mix_hard", 2.0),
            },
        }
        kit_master_on_ids = _read_kit_master_on_ids(form)
        kit_prefill = _kit_qty_prefill_from_form(form)
        return templates.TemplateResponse(
            "work_products_new.html",
            _ctx(
                request,
                current_user=current_user,
                error=str(exc),
                fp=fp,
                masters=masters,
                kit_master_on_ids=kit_master_on_ids,
                kit_table_state_json=_kit_table_state_json(current_user, masters, kit_prefill, db),
                default_date=date.today().isoformat(),
                kinds=[{"value": k.value, "label": _kind_label(k)} for k in WorkKind],
                scopes=[
                    {"value": s.value, "label": ("В наличие" if s == WorkScope.IN_STOCK else "На заказ")}
                    for s in WorkScope
                ],
                kit_se_items=_kit_se_items(),
                kit_de_items=_kit_de_items(),
                rubber_types=[{"value": v, "label": l} for v, l in _rubber_type_items()],
                work_price_meta_json=json.dumps(work_price_meta, ensure_ascii=False),
            ),
            status_code=400,
        )


@router.get("", response_class=HTMLResponse)
def work_list(
    request: Request,
    current_user: AuthUser = _VIEW,
    db: Session = Depends(get_db),
):
    msg = request.query_params.get("msg")
    stmt = select(WorkForInventory).options(
        selectinload(WorkForInventory.client),
        selectinload(WorkForInventory.staff_rows).selectinload(WorkForInventoryStaff.user),
    )
    if current_user.role == UserRole.MASTER:
        stmt = (
            stmt.outerjoin(WorkForInventoryStaff, WorkForInventoryStaff.work_id == WorkForInventory.id)
            .where(
                (WorkForInventory.created_by_user_id == current_user.id)
                | (WorkForInventoryStaff.user_id == current_user.id)
            )
            .distinct()
        )
    stmt = stmt.order_by(WorkForInventory.id.desc()).limit(100)
    rows = list(db.scalars(stmt).all())
    return templates.TemplateResponse(
        "work_products_list.html",
        _ctx(request, current_user=current_user, rows=rows, msg=msg, can_create=(current_user.role == UserRole.MASTER)),
    )


@router.get("/{work_id}", response_class=HTMLResponse)
def work_detail(
    request: Request,
    work_id: int,
    current_user: AuthUser = _VIEW,
    db: Session = Depends(get_db),
):
    w = db.get(WorkForInventory, work_id)
    if not w:
        return templates.TemplateResponse(
            "work_products_detail.html",
            _ctx(request, current_user=current_user, row=None, err="Работа не найдена."),
            status_code=404,
        )
    # load relations for template
    w = db.scalar(
        select(WorkForInventory)
        .options(
            selectinload(WorkForInventory.client),
            selectinload(WorkForInventory.staff_rows).selectinload(WorkForInventoryStaff.user),
        )
        .where(WorkForInventory.id == work_id)
    )
    if current_user.role == UserRole.MASTER:
        allowed = (w.created_by_user_id == current_user.id) or any(
            s.user_id == current_user.id for s in (w.staff_rows or [])
        )
        if not allowed:
            return templates.TemplateResponse(
                "work_products_detail.html",
                _ctx(request, current_user=current_user, row=None, err="Недостаточно прав для просмотра этой работы."),
                status_code=403,
            )
    can_edit = (current_user.role == UserRole.ADMIN_SUPER)
    edit_allowed, edit_block_msg = _work_edit_allowed(db, w) if can_edit else (False, "")
    void_msg = request.query_params.get("msg")
    details = _details_obj(w.details_json)
    audit_rows = list(
        db.scalars(
            select(WorkForInventoryAuditLog)
            .where(WorkForInventoryAuditLog.work_id == w.id)
            .order_by(WorkForInventoryAuditLog.changed_at.desc(), WorkForInventoryAuditLog.id.desc())
            .limit(200)
        ).all()
    )
    return templates.TemplateResponse(
        "work_products_detail.html",
        _ctx(
            request,
            current_user=current_user,
            row=w,
            details=details,
            err=None,
            saved=(request.query_params.get("msg") == "saved"),
            can_edit=can_edit,
            edit_allowed=edit_allowed,
            edit_block_msg=edit_block_msg,
            audit_rows=audit_rows,
            msg=void_msg,
        ),
    )


def _kit_has_any_usage(db: Session, kit_id: int) -> bool:
    sale_exists = db.scalar(
        select(ProductSale.id).where(ProductSale.kit_id == kit_id, ProductSale.is_voided.is_(False)).limit(1)
    )
    if sale_exists is not None:
        return True
    visit_exists = db.scalar(
        select(VisitKitUsage.id)
        .join(Visit, VisitKitUsage.visit_id == Visit.id)
        .where(VisitKitUsage.kit_id == kit_id, Visit.is_cancelled.is_(False))
        .limit(1)
    )
    if visit_exists is not None:
        return True
    return False


@router.post("/{work_id}/void")
async def work_void(
    work_id: int,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    w = db.get(WorkForInventory, work_id)
    if not w:
        return RedirectResponse(url="/sales/work?msg=not_found", status_code=303)
    if w.is_voided:
        return RedirectResponse(url=f"/sales/work/{work_id}?msg=already_voided", status_code=303)

    ok, _ = _work_edit_allowed(db, w)
    if not ok:
        return RedirectResponse(url=f"/sales/work/{work_id}?msg=void_blocked", status_code=303)

    kit = None
    if w.created_kit_id:
        kit = db.get(Kit, int(w.created_kit_id))
        if not kit:
            return RedirectResponse(url=f"/sales/work/{work_id}?msg=void_conflict", status_code=303)
        if int(kit.pieces_available) != int(kit.pieces_total):
            return RedirectResponse(url=f"/sales/work/{work_id}?msg=void_conflict", status_code=303)
        if _kit_has_any_usage(db, kit.id):
            return RedirectResponse(url=f"/sales/work/{work_id}?msg=void_conflict", status_code=303)

    storno_source_accruals(db, PayrollFundSourceKind.WORK, w.id, current_user.id)

    before = SimpleNamespace(
        is_voided=w.is_voided,
        voided_at=getattr(w, "voided_at", None),
        voided_by_user_id=getattr(w, "voided_by_user_id", None),
    )
    w.is_voided = True
    w.voided_at = datetime.utcnow()
    w.voided_by_user_id = current_user.id
    w.updated_at = datetime.utcnow()
    w.updated_by_user_id = current_user.id

    if kit:
        kit.is_archived = True
        kit.is_in_stock = False
        kit.pieces_available = 0

    write_audit_rows(
        db,
        log_model=WorkForInventoryAuditLog,
        entity_field="work_id",
        entity_id=w.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before, w, ("is_voided", "voided_at", "voided_by_user_id")),
    )
    db.commit()
    return RedirectResponse(url=f"/sales/work/{work_id}?msg=voided", status_code=303)


@router.get("/{work_id}/edit", response_class=HTMLResponse)
def work_edit_form(
    request: Request,
    work_id: int,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    w = db.scalar(
        select(WorkForInventory)
        .options(
            selectinload(WorkForInventory.client),
            selectinload(WorkForInventory.staff_rows).selectinload(WorkForInventoryStaff.user),
        )
        .where(WorkForInventory.id == work_id)
    )
    if not w:
        return templates.TemplateResponse(
            "work_products_edit.html",
            _ctx(request, current_user=current_user, row=None, err="Работа не найдена."),
            status_code=404,
        )
    edit_allowed, edit_block_msg = _work_edit_allowed(db, w)
    if not edit_allowed:
        return templates.TemplateResponse(
            "work_products_edit.html",
            _ctx(request, current_user=current_user, row=w, err=edit_block_msg),
            status_code=403,
        )
    return templates.TemplateResponse(
        "work_products_edit.html",
        _ctx(request, current_user=current_user, row=w, err=None),
    )


@router.post("/{work_id}/edit")
async def work_edit_save(
    request: Request,
    work_id: int,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    w = db.scalar(
        select(WorkForInventory)
        .options(
            selectinload(WorkForInventory.client),
            selectinload(WorkForInventory.staff_rows).selectinload(WorkForInventoryStaff.user),
        )
        .where(WorkForInventory.id == work_id)
    )
    if not w:
        return RedirectResponse(url=f"/sales/work/{work_id}", status_code=303)
    edit_allowed, edit_block_msg = _work_edit_allowed(db, w)
    if not edit_allowed:
        return templates.TemplateResponse(
            "work_products_edit.html",
            _ctx(request, current_user=current_user, row=w, err=edit_block_msg),
            status_code=403,
        )
    form = await request.form()

    def _p_float(name: str, default: float) -> float:
        try:
            return float(str(form.get(name) or str(default)).strip().replace(",", "."))
        except ValueError:
            raise ValueError(f"Некорректное число: {name}")

    def _p_int_opt(name: str) -> int | None:
        raw = (str(form.get(name) or "")).strip()
        if not raw:
            return None
        try:
            v = int(float(raw.replace(",", ".")))
        except ValueError:
            raise ValueError(f"Некорректное число: {name}")
        if v < 0:
            raise ValueError(f"Значение не может быть отрицательным: {name}")
        return v

    try:
        before = SimpleNamespace(
            amount_from_client=w.amount_from_client,
            comment=w.comment,
            kanekalon_grams=w.kanekalon_grams,
            kudri_grams=w.kudri_grams,
            materials_cost_total=w.materials_cost_total,
            extra_costs_amount=w.extra_costs_amount,
            cost_total_amount=w.cost_total_amount,
            master_profit_amount=w.master_profit_amount,
            studio_profit_amount=w.studio_profit_amount,
            profit_total_amount=w.profit_total_amount,
        )
        # base fields
        w.amount_from_client = _p_int_opt("amount_from_client")
        w.comment = (_g_str(form, "comment", "") or "").strip() or None

        w.kanekalon_grams = max(0.0, _p_float("kanekalon_grams", float(w.kanekalon_grams or 0.0)))
        w.kudri_grams = max(0.0, _p_float("kudri_grams", float(w.kudri_grams or 0.0)))
        w.materials_cost_total = max(0.0, _p_float("materials_cost_total", float(w.materials_cost_total or 0.0)))
        w.extra_costs_amount = max(0.0, _p_float("extra_costs_amount", float(w.extra_costs_amount or 0.0)))
        w.cost_total_amount = max(0.0, _p_float("cost_total_amount", float(w.cost_total_amount or 0.0)))

        w.master_profit_amount = max(0.0, _p_float("master_profit_amount", float(w.master_profit_amount or 0.0)))
        w.studio_profit_amount = max(0.0, _p_float("studio_profit_amount", float(w.studio_profit_amount or 0.0)))
        w.profit_total_amount = max(0.0, _p_float("profit_total_amount", float(w.profit_total_amount or 0.0)))
    except ValueError as exc:
        return templates.TemplateResponse(
            "work_products_edit.html",
            _ctx(request, current_user=current_user, row=w, err=str(exc)),
            status_code=400,
        )

    w.updated_at = datetime.utcnow()
    w.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=WorkForInventoryAuditLog,
        entity_field="work_id",
        entity_id=w.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(
            before,
            w,
            (
                "amount_from_client",
                "comment",
                "kanekalon_grams",
                "kudri_grams",
                "materials_cost_total",
                "extra_costs_amount",
                "cost_total_amount",
                "master_profit_amount",
                "studio_profit_amount",
                "profit_total_amount",
            ),
        ),
    )
    staff_rows = list(
        db.scalars(select(WorkForInventoryStaff).where(WorkForInventoryStaff.work_id == w.id)).all()
    )
    replace_work_accruals(db, w.id, staff_rows, current_user.id)
    db.commit()
    return RedirectResponse(url=f"/sales/work/{work_id}?msg=saved", status_code=303)

