"""
Работа с товарами: единая форма (в наличие / на заказ) + запись в work_for_inventory.
Этап 6.3.2: каркас, без детальных расчётов по видам.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.payroll_fund import post_work_accruals, replace_work_accruals, storno_source_accruals
from starlette.datastructures import UploadFile

from app.auth import AuthUser, require_role
from app.client_validation import format_created_by_label
from app.db.models import (
    Client,
    Kit,
    KitAuthorStaff,
    CatalogProduct,
    KitReserve,
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
    WorkRate,
    WorkScope,
)
from app.db.session import get_db
from app.user_roles import select_users_with_role, user_has_role
from app.kit_composition import (
    KIT_INVENTORY_PIECE_EXCLUDE_KEYS,
    composition_json_from_totals,
)
from app.kit_inlay_visit import _materials_cost_and_snapshot
from app.work_products_compute import compute_work_financials
from app.forms_parse import parse_bool, parse_date_iso, parse_float, parse_int
from app.work_rate_keys import CUSTOM_ORDER_BONUS_MULTIPLIER, STUDIO_SHARE
from app.time_utils import utcnow_naive

_WORK_NEW_FP_KEYS = frozenset({
    "booking_id",
    "client_id",
    "performed_date",
    "scope",
    "kind",
    "amount_from_client",
    "comment",
    "kanekalon_grams",
    "kudri_grams",
    "mix_source",
    "mix_complexity",
    "rubber_type",
    "rubber_attach_qty",
    "rubber_braids_qty",
    "corr_trim_qty",
    "corr_hourly_hours",
    "corr_kit_description",
    "corr_kit_blanks_count",
    "corr_wash",
    "corr_steam",
    "corr_circle",
})
from app.visit_edit_policy import (
    edit_window_days,
    ensure_event_date_in_open_payroll_period,
    is_in_closed_payroll_period,
    within_edit_window,
)
from app.ru_labels import ru_user_role
from app.audit import diff_fields, write_audit_rows
from app.kit_crud import kit_key_excluded_from_client_price
from app.mix_rates import mix_complexity_rate_for, mix_rates_meta_json_dict
from app.ui_visit_display import ru_mix_complexity as ru_mix_complexity_label
from app.zakaz_blanks import kit_form_blank_defs

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["ru_user_role"] = ru_user_role
router = APIRouter(prefix="/sales/work", tags=["work-products"])
# GET-алиас под старые закладки/ссылки (если где-то фигурировал /admin/sales/...):
#   /admin/sales/work/...  -> 308 -> /sales/work/...
legacy_admin_router = APIRouter(prefix="/admin/sales/work", tags=["work-products-legacy"])
_VIEW = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER))
_MASTER = Depends(require_role(UserRole.MASTER))
_SUPER = Depends(require_role(UserRole.ADMIN_SUPER))


def _redirect_admin_sales_work_to_canon(request: Request, *, suffix: str = "") -> RedirectResponse:
    """Старые URL /admin/sales/work → канон /sales/work (GET, 308, query сохраняется)."""
    suf = (suffix or "").strip()
    if suf and not suf.startswith("/"):
        suf = f"/{suf}"
    new_path = f"/sales/work{suf}"
    return RedirectResponse(url=str(request.url.replace(path=new_path)), status_code=308)


@legacy_admin_router.get("", response_class=HTMLResponse)
def work_list_legacy_redirect(
    request: Request,
    current_user: AuthUser = _VIEW,
):
    return _redirect_admin_sales_work_to_canon(request)


@legacy_admin_router.get("/new", response_class=HTMLResponse)
def work_new_get_legacy_redirect(
    request: Request,
    current_user: AuthUser = _MASTER,
):
    return _redirect_admin_sales_work_to_canon(request, suffix="/new")


@legacy_admin_router.get("/{work_id}/edit", response_class=HTMLResponse)
def work_edit_form_legacy_redirect(
    work_id: int,
    request: Request,
    current_user: AuthUser = _SUPER,
):
    return _redirect_admin_sales_work_to_canon(request, suffix=f"/{int(work_id)}/edit")


@legacy_admin_router.get("/{work_id}", response_class=HTMLResponse)
def work_detail_legacy_redirect(
    work_id: int,
    request: Request,
    current_user: AuthUser = _VIEW,
):
    return _redirect_admin_sales_work_to_canon(request, suffix=f"/{int(work_id)}")


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
    s = _g_str(form, name, "")
    if not s:
        return default
    try:
        return parse_float(s, default=default, field_name=name)
    except ValueError:
        return default


def _g_bool(form: Any, name: str) -> bool:
    v = form.get(name)
    if v is None or isinstance(v, UploadFile):
        return False
    s = v.decode() if isinstance(v, (bytes, bytearray)) else v
    return parse_bool(s)


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
            "kitWorkPayByKey": _kit_work_pay_map_from_catalog(db),
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
    r = db.scalar(select(WorkRate).where(WorkRate.key == STUDIO_SHARE, WorkRate.is_active.is_(True)))
    if not r:
        return 0.50
    try:
        return float(json.loads(r.value_json))
    except Exception:
        return 0.50


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
        ("TAIL_CRAB_MINI", "Хвост на крабе — mini"),
        ("TAIL_CRAB_STANDARD", "Хвост на крабе — standard"),
        ("TAIL_CRAB_MAX", "Хвост на крабе — max"),
        ("TAIL_NET_MINI", "Хвост на сетке — mini"),
        ("TAIL_NET_STANDARD", "Хвост на сетке — standard"),
        ("TAIL_NET_MAX", "Хвост на сетке — max"),
        ("TAIL_BUN_MINI", "Хвост на бублике — mini"),
        ("TAIL_BUN_STANDARD", "Хвост на бублике — standard"),
        ("TAIL_BUN_MAX", "Хвост на бублике — max"),
        ("BRAIDS_ELASTIC", "Косы на резинке"),
    ]


def _rubber_service_name(rubber_type: str) -> str:
    return {
        "TAIL_ELASTIC": "Хвост на резинке (1 крепление)",
        "TAIL_CRAB_MINI": "Хвост на крабе — mini",
        "TAIL_CRAB_STANDARD": "Хвост на крабе — standard",
        "TAIL_CRAB_MAX": "Хвост на крабе — max",
        "TAIL_NET_MINI": "Хвост на сетке — mini",
        "TAIL_NET_STANDARD": "Хвост на сетке — standard",
        "TAIL_NET_MAX": "Хвост на сетке — max",
        "TAIL_BUN_MINI": "Хвост на бублике — mini",
        "TAIL_BUN_STANDARD": "Хвост на бублике — standard",
        "TAIL_BUN_MAX": "Хвост на бублике — max",
        "BRAIDS_ELASTIC": "Косы на резинке (1 коса)",
    }[rubber_type]


def _rubber_pricing_from_catalog(db: Session, rubber_type: str) -> tuple[float, float, float, bool, str | None]:
    """
    Возвращает: (master_pay, studio_pay, fixed_expense, is_per_unit, unit_label).
    Берём из прайса товаров (catalog_products): категория «Заказ» → подкатегория «Хвосты/резинки».
    """
    svc_name = _rubber_service_name(rubber_type)
    row = db.scalar(
        select(CatalogProduct).where(
            CatalogProduct.category_name == "Заказ",
            CatalogProduct.subcategory_name == "Хвосты/резинки",
            CatalogProduct.name == svc_name,
            CatalogProduct.is_active.is_(True),
        )
    )
    if not row:
        raise ValueError(f"Не найден прайс для «{svc_name}».")
    try:
        meta = json.loads(row.meta_json or "{}")
    except Exception:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    mp = float(meta.get("master_pay") or 0.0)
    sp = float(meta.get("studio_pay") or 0.0)
    fx = float(meta.get("fixed_expense") or 0.0)
    is_per_unit = bool(meta.get("is_per_unit") or False)
    unit_label = meta.get("unit_label") or None
    return mp, sp, fx, is_per_unit, (str(unit_label) if unit_label else None)


def _zakaz_subcategory_services_map(
    db: Session, subcategory_name: str
) -> dict[str, dict[str, float | bool | None]]:
    """
    Возвращает map по имени услуги в подкатегории:
    { name: {client_price, master_pay, studio_pay, fixed_expense, is_per_unit} }
    """
    rows = list(
        db.scalars(
            select(CatalogProduct).where(
                CatalogProduct.category_name == "Заказ",
                CatalogProduct.subcategory_name == subcategory_name,
                CatalogProduct.is_active.is_(True),
            )
        ).all()
    )
    out: dict[str, dict[str, float | bool | None]] = {}
    for r in rows:
        try:
            meta = json.loads(r.meta_json or "{}")
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        cp = float(r.price) if r.price is not None else None
        out[r.name] = {
            "client_price": cp,
            "client_from": cp,
            "client_to": cp,
            "master_pay": float(meta.get("master_pay")) if meta.get("master_pay") is not None else None,
            "studio_pay": float(meta.get("studio_pay")) if meta.get("studio_pay") is not None else None,
            "fixed_expense": float(meta.get("fixed_expense")) if meta.get("fixed_expense") is not None else None,
            "is_per_unit": bool(meta.get("is_per_unit") or False),
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


def _kind_label(k: WorkKind) -> str:
    return {
        WorkKind.KIT: "Комплект/Заготовки (поштучно)",
        WorkKind.MIX: "Смешка",
        WorkKind.RUBBER: "Хвосты/резинки",
        WorkKind.KIT_CORRECTION: "Коррекция комплекта",
        WorkKind.HAIR_EXT_PREP: "Подготовка к наращиванию волос (заглушка)",
    }[k]


def _kit_se_items() -> list[tuple[str, str]]:
    return [(row.key or "", row.display_name) for row in kit_form_blank_defs("SE") if row.key]


def _kit_de_items() -> list[tuple[str, str]]:
    return [(row.key or "", row.display_name) for row in kit_form_blank_defs("DE") if row.key]


def _kit_meta_by_kit_key(db: Session) -> dict[str, dict[str, Any]]:
    rows = list(
        db.scalars(
            select(CatalogProduct).where(
                CatalogProduct.category_name == "Заказ",
                CatalogProduct.subcategory_name == "Заготовки поштучно",
                CatalogProduct.is_active.is_(True),
            )
        ).all()
    )
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        try:
            meta = json.loads(r.meta_json or "{}")
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        k = str(meta.get("kit_key") or "").strip()
        if k:
            out[k] = meta
    return out


def _kit_work_pay_map_from_catalog(db: Session) -> dict[str, float]:
    rows = list(
        db.scalars(
            select(CatalogProduct).where(
                CatalogProduct.category_name == "Заказ",
                CatalogProduct.subcategory_name == "Заготовки поштучно",
                CatalogProduct.is_active.is_(True),
            )
        ).all()
    )
    out: dict[str, float] = {}
    for r in rows:
        try:
            meta = json.loads(r.meta_json or "{}")
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        k = str(meta.get("kit_key") or "").strip()
        if not k:
            continue
        if bool(meta.get("is_bu")):
            continue
        out[k] = float(meta.get("master_pay") or 0.0)
    return out


def _kit_work_pay_for_item(db: Session, item_key: str) -> float:
    return float(_kit_work_pay_map_from_catalog(db).get(item_key) or 0.0)


def _kit_price_map_from_catalog(db: Session) -> dict[str, float]:
    rows = list(
        db.scalars(
            select(CatalogProduct).where(
                CatalogProduct.category_name == "Заказ",
                CatalogProduct.subcategory_name == "Заготовки поштучно",
                CatalogProduct.is_active.is_(True),
            )
        ).all()
    )
    out: dict[str, float] = {}
    for r in rows:
        try:
            meta = json.loads(r.meta_json or "{}")
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        k = (meta.get("kit_key") or "").strip()
        if not k:
            continue
        if r.price is None:
            continue
        out[k] = float(r.price)
    return out


def _kit_catalog_rows_by_key(db: Session) -> dict[str, dict[str, Any]]:
    rows = list(
        db.scalars(
            select(CatalogProduct).where(
                CatalogProduct.category_name == "Заказ",
                CatalogProduct.subcategory_name == "Заготовки поштучно",
                CatalogProduct.is_active.is_(True),
            )
        ).all()
    )
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        try:
            meta = json.loads(r.meta_json or "{}")
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        k = (meta.get("kit_key") or "").strip()
        if not k:
            continue
        out[k] = {
            "name": r.name,
            "price": (float(r.price) if r.price is not None else None),
        }
    return out


def _kit_item_labels_map() -> dict[str, str]:
    return {k: lbl for k, lbl in (_kit_se_items() + _kit_de_items())}


def _fmt_money(v: float) -> str:
    return f"{float(v):.2f} ₽"


def _kit_client_stock_price_total(db: Session, *, kit_totals: dict[str, int], extra_costs_amount: float) -> float:
    price_map = _kit_price_map_from_catalog(db)
    meta_by_key = _kit_meta_by_kit_key(db)
    missing: list[str] = []
    total = 0.0
    for k, q in kit_totals.items():
        q = int(q)
        if q <= 0:
            continue
        if kit_key_excluded_from_client_price(meta_by_key.get(k) or {}, k):
            continue
        p = price_map.get(k)
        if p is None:
            missing.append(k)
            continue
        total += float(p) * float(q)
    if missing:
        miss = ", ".join(sorted(set(missing)))
        raise ValueError(f"Не найдены цены в прайсе «Заказ → Заготовки поштучно» для: {miss}.")
    return float(total) + float(max(0.0, extra_costs_amount))


def _kit_stock_price_snapshot_text(
    db: Session, *, kit_totals: dict[str, int], extra_costs_amount: float
) -> str:
    catalog = _kit_catalog_rows_by_key(db)
    labels = _kit_item_labels_map()
    meta_by_key = _kit_meta_by_kit_key(db)
    missing: list[str] = []
    lines = ["Расчёт цены комплекта:"]
    total = 0.0
    for key in sorted(kit_totals.keys()):
        qty = int(kit_totals.get(key, 0) or 0)
        if qty <= 0:
            continue
        if kit_key_excluded_from_client_price(meta_by_key.get(key) or {}, key):
            continue
        row = catalog.get(key)
        price = None if row is None else row.get("price")
        if price is None:
            missing.append(key)
            continue
        line_total = float(price) * float(qty)
        total += line_total
        title = str((row or {}).get("name") or labels.get(key) or key)
        lines.append(f"{title} — {qty} шт × {_fmt_money(float(price))} = {_fmt_money(line_total)}")
    if extra_costs_amount > 0:
        total += float(extra_costs_amount)
        lines.append(f"Доп. расходы — {_fmt_money(float(extra_costs_amount))}")
    lines.append(f"Итого — {_fmt_money(total)}")
    if missing:
        lines.append("")
        lines.append("Нет цен для ключей: " + ", ".join(sorted(set(missing))))
    return "\n".join(lines)


def _kit_cost_snapshot_text(
    db: Session,
    *,
    kit_totals: dict[str, int],
    mat_cost: float,
    kanek: float,
    kudri: float,
    k_snap: float,
    ku_snap: float,
    mix_source: MixSource | None,
    mix_complexity: MixComplexity | None,
    grams_total: float,
    extra_costs_amount: float,
) -> str:
    labels = _kit_item_labels_map()
    lines = ["Расчёт себестоимости:"]
    total = 0.0

    wage_total = 0.0
    for key in sorted(kit_totals.keys()):
        qty = int(kit_totals.get(key, 0) or 0)
        if qty <= 0:
            continue
        rate = _kit_work_pay_for_item(db, key)
        if rate <= 0:
            continue
        row_total = float(rate) * float(qty)
        wage_total += row_total
        lines.append(f"{labels.get(key) or key} — {qty} шт — ЗП {_fmt_money(row_total)}")

    if mix_source == MixSource.SELF_MIXED and grams_total > 0 and mix_complexity is not None:
        mix_rate = mix_complexity_rate_for(db, mix_complexity)
        mix_pay = float(grams_total) * float(mix_rate)
        if mix_pay > 0:
            wage_total += mix_pay
            lines.append(
                f"Смешка ({ru_mix_complexity_label(mix_complexity.value)}) — {grams_total:.0f} г × {_fmt_money(float(mix_rate)).replace(' ₽', ' ₽/г')} = {_fmt_money(mix_pay)}"
            )

    if wage_total > 0:
        total += wage_total

    if kanek > 0:
        lines.append(
            f"Материал: канекалон — {kanek:.0f} г × {_fmt_money(float(k_snap)).replace(' ₽', ' ₽/г')} = {_fmt_money(float(kanek) * float(k_snap))}"
        )
    if kudri > 0:
        lines.append(
            f"Материал: кудри — {kudri:.0f} г × {_fmt_money(float(ku_snap)).replace(' ₽', ' ₽/г')} = {_fmt_money(float(kudri) * float(ku_snap))}"
        )
    if mat_cost > 0:
        total += float(mat_cost)
    if extra_costs_amount > 0:
        total += float(extra_costs_amount)
        lines.append(f"Доп. расходы — {_fmt_money(float(extra_costs_amount))}")
    lines.append(f"Итого — {_fmt_money(total)}")
    return "\n".join(lines)


@router.get("/new", response_class=HTMLResponse)
def work_new_get(
    request: Request,
    current_user: AuthUser = _MASTER,
    db: Session = Depends(get_db),
):
    fp: dict[str, str] = {}
    selected_client = None
    cid = str(request.query_params.get("client_id") or "").strip()
    try:
        cid_i = parse_int(cid, min=1, field_name="client_id") if cid else 0
    except ValueError:
        cid_i = 0
    if cid_i > 0:
        fp["client_id"] = str(cid_i)
        selected_client = db.get(Client, cid_i)
    bid = str(request.query_params.get("booking_id") or "").strip()
    try:
        bid_i = parse_int(bid, min=1, field_name="booking_id") if bid else 0
    except ValueError:
        bid_i = 0
    if bid_i > 0:
        fp["booking_id"] = str(bid_i)
    for key in _WORK_NEW_FP_KEYS:
        v = request.query_params.get(key)
        if v is not None and str(v).strip() != "":
            fp[key] = str(v).strip()
    masters = _list_masters_for_work_form(db)
    work_price_meta = {
        "rubber": _zakaz_subcategory_services_map(db, "Хвосты/резинки"),
        "correction": _zakaz_subcategory_services_map(db, "Коррекция комплекта"),
        "customOrderBonus": _wr_float(db, CUSTOM_ORDER_BONUS_MULTIPLIER, 1.0),
        "mixRates": mix_rates_meta_json_dict(db),
    }
    return templates.TemplateResponse(
        "work_products_new.html",
        _ctx(
            request,
            current_user=current_user,
            error=None,
            fp=fp,
            selected_client=selected_client,
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
@legacy_admin_router.post("/new")
async def work_new_post(
    request: Request,
    current_user: AuthUser = _MASTER,
    db: Session = Depends(get_db),
):
    form = await request.form()
    fp = {k: _g_str(form, k) for k in form.keys() if isinstance(k, str)}
    try:
        pd_raw = (_g_str(form, "performed_date", "") or "").strip()
        try:
            performed_dt = datetime.combine(parse_date_iso(pd_raw, field_name="performed_date"), datetime.min.time())
        except ValueError:
            raise ValueError("Некорректная дата работы.")
        ensure_event_date_in_open_payroll_period(db, performed_dt)
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
            try:
                client_id = parse_int(cid_raw, min=1, field_name="client_id")
            except ValueError:
                raise ValueError("Для режима «на заказ» выберите клиента.")
            if not db.get(Client, client_id):
                raise ValueError("Клиент не найден.")
            afc_raw = (_g_str(form, "amount_from_client", "") or "").strip()
            if afc_raw:
                try:
                    afc = parse_float(afc_raw, min=0.0, field_name="amount_from_client")
                    amount_from_client = int(afc)
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
            mc_raw = (_g_str(form, "mix_complexity", "") or "").strip().upper()
            mc_raw = {"SIMPLE": "STANDARD", "MEDIUM": "KANEK", "HARD": "THERMO"}.get(mc_raw, mc_raw)
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
        corr_hourly_hours = 0.0
        corr_wash = False
        corr_circle = False
        corr_steam = False
        # KIT: parse blanks + compute master/studio profit
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
            corr_hourly_hours = max(0.0, _g_float(form, "corr_hourly_hours", 0.0))
            corr_wash = _g_bool(form, "corr_wash")
            corr_circle = _g_bool(form, "corr_circle")
            corr_steam = _g_bool(form, "corr_steam")
            corr_kit_description = (_g_str(form, "corr_kit_description", "") or "").strip() or None
            raw_blanks = (_g_str(form, "corr_kit_blanks_count", "") or "").strip()
            corr_kit_blanks_count: int | None = None
            if raw_blanks:
                try:
                    corr_kit_blanks_count = int(parse_float(raw_blanks, min=0.0, field_name="corr_kit_blanks_count"))
                except ValueError:
                    raise ValueError("«Количество заготовок в комплекте» — целое число.")
                if corr_kit_blanks_count < 0:
                    raise ValueError("«Количество заготовок в комплекте» не может быть отрицательным.")

            if corr_wash and corr_circle:
                raise ValueError("Если выбрана «Стирка», то «Одевание на круг» выбирать нельзя (входит в стирку).")

            if corr_trim_qty < 0:
                raise ValueError("Количество должно быть неотрицательным числом.")

            has_note = bool(corr_kit_description) or (corr_kit_blanks_count is not None)
            if (
                (corr_trim_qty <= 0)
                and (corr_hourly_hours <= 0)
                and (not corr_wash)
                and (not corr_circle)
                and (not corr_steam)
                and (not has_note)
            ):
                raise ValueError("Для «Коррекция комплекта» укажите хотя бы одну операцию или комментарий/учёт заготовок.")

            details["correction"] = {
                "trim_qty": corr_trim_qty,
                "hourly_hours": float(corr_hourly_hours),
                "wash": corr_wash,
                "circle": corr_circle,
                "steam": corr_steam,
            }
            if corr_kit_description:
                details["correction"]["kit_description"] = corr_kit_description
            if corr_kit_blanks_count is not None:
                details["correction"]["kit_blanks_count"] = corr_kit_blanks_count
            alloc = [(current_user.id, 1.0)]
        else:
            alloc = [(current_user.id, 1.0)]

        fin = compute_work_financials(
            db,
            kind=kind,
            scope=scope,
            alloc=alloc,
            current_user_id=current_user.id,
            mat_cost=mat_cost,
            kit_totals=kit_totals,
            kit_staff_ids=kit_staff_ids,
            kit_by_staff=kit_by_staff,
            mix_source=mix_source,
            mix_complexity=mix_complexity,
            grams_total=grams_total,
            rubber_type=rubber_type,
            rubber_qty=rubber_qty,
            corr_trim_qty=corr_trim_qty,
            corr_hourly_hours=float(corr_hourly_hours),
            corr_hourly_avg=False,
            corr_wash=corr_wash,
            corr_circle=corr_circle,
            corr_steam=corr_steam,
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
            performed_date=performed_dt,
            kind=kind,
            scope=scope,
            client_id=client_id,
            amount_from_client=amount_from_client,
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
        bid_raw = (_g_str(form, "booking_id", "") or "").strip()
        bid_for_auto_complete: int | None = None
        try:
            bid_i = parse_int(bid_raw, min=1, field_name="booking_id") if bid_raw else 0
        except ValueError:
            bid_i = 0
        if bid_i > 0:
            work.booking_id = bid_i
            bid_for_auto_complete = bid_i
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

        # Create Kit for KIT work:
        # - IN_STOCK: обычная складская карточка
        # - CUSTOM_ORDER: создаём складскую карточку и сразу полностью резервируем за клиентом
        if kind == WorkKind.KIT and scope in (WorkScope.IN_STOCK, WorkScope.CUSTOM_ORDER):
            sku = (_g_str(form, "kit_sku", "") or "").strip()
            title = (_g_str(form, "kit_title", "") or "").strip()
            if scope == WorkScope.IN_STOCK:
                if not sku:
                    raise ValueError("Для «в наличие» укажите артикул комплекта.")
                if not title:
                    raise ValueError("Для «в наличие» укажите название комплекта.")
                if db.scalar(select(Kit.id).where(Kit.sku == sku)):
                    raise ValueError("Комплект с таким артикулом уже есть — укажите другой.")
            else:
                # На заказ: артикул/название можно не вводить вручную.
                if not sku:
                    sku = f"ORDER-{work.id}"
                if not title:
                    cl = db.get(Client, client_id) if client_id else None
                    title = f"Заказ — комплект (клиент {cl.name if cl else client_id})"
                # Если внезапно пересекается (крайне редко), дополним временем.
                if db.scalar(select(Kit.id).where(Kit.sku == sku)):
                    sku = f"{sku}-{int(utcnow_naive().timestamp())}"

            full_cost = float(cost_total_amount) + float(master_total)
            comp_json = composition_json_from_totals(kit_totals)
            stock_price_total = _kit_client_stock_price_total(
                db, kit_totals=kit_totals, extra_costs_amount=float(extra_costs_amount)
            )
            stock_price_snapshot_text = _kit_stock_price_snapshot_text(
                db, kit_totals=kit_totals, extra_costs_amount=float(extra_costs_amount)
            )
            cost_snapshot_text = _kit_cost_snapshot_text(
                db,
                kit_totals=kit_totals,
                mat_cost=float(mat_cost),
                kanek=float(kanek),
                kudri=float(kudri),
                k_snap=float(k_snap),
                ku_snap=float(ku_snap),
                mix_source=mix_source,
                mix_complexity=mix_complexity,
                grams_total=float(grams_total),
                extra_costs_amount=float(extra_costs_amount),
            )
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
                stock_price_total=float(stock_price_total),
                composition_json=comp_json,
                stock_price_snapshot_text=stock_price_snapshot_text,
                discount_percent=0,
                cost_total=full_cost,
                cost_snapshot_text=cost_snapshot_text,
                author_cost_total=None,
                created_at=utcnow_naive(),
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

            if scope == WorkScope.CUSTOM_ORDER and client_id:
                pieces_reserved = int(kit.pieces_total)
                db.add(
                    KitReserve(
                        kit_id=kit.id,
                        pieces_reserved=pieces_reserved,
                        reserved_at=utcnow_naive(),
                        reserved_by_user_id=int(current_user.id),
                        reserved_for_client_id=int(client_id),
                        reserved_for_user_id=None,
                    )
                )
                # Как при ручном резерве в админке: свободный остаток уменьшается на объём резерва.
                kit.pieces_available = max(
                    0, int(kit.pieces_available or 0) - pieces_reserved
                )

        staff_saved = list(
            db.scalars(
                select(WorkForInventoryStaff).where(WorkForInventoryStaff.work_id == work.id)
            ).all()
        )
        post_work_accruals(db, work.id, staff_saved, current_user.id)
        db.commit()
        if bid_for_auto_complete is not None:
            from app.routes.bookings import try_auto_complete_booking

            try_auto_complete_booking(db, bid_for_auto_complete)
            db.commit()
        return RedirectResponse(url="/sales/work?msg=saved", status_code=303)
    except ValueError as exc:
        masters = _list_masters_for_work_form(db)
        work_price_meta = {
            "rubber": _zakaz_subcategory_services_map(db, "Хвосты/резинки"),
            "correction": _zakaz_subcategory_services_map(db, "Коррекция комплекта"),
            "customOrderBonus": _wr_float(db, CUSTOM_ORDER_BONUS_MULTIPLIER, 1.0),
            "mixRates": mix_rates_meta_json_dict(db),
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
    mine: str | None = Query(None),
    current_user: AuthUser = _VIEW,
    db: Session = Depends(get_db),
):
    msg = request.query_params.get("msg")
    mine_raw = (mine or "").strip().lower()
    if current_user.role == UserRole.MASTER:
        if mine_raw in ("0", "false", "no", "all"):
            work_mine_only = False
        elif mine_raw in ("1", "true", "yes", "only"):
            work_mine_only = True
        else:
            work_mine_only = True
    else:
        work_mine_only = mine_raw in ("1", "true", "yes", "only")

    stmt = select(WorkForInventory).options(
        selectinload(WorkForInventory.client),
        selectinload(WorkForInventory.staff_rows).selectinload(WorkForInventoryStaff.user),
    )
    if work_mine_only:
        stmt = (
            stmt.outerjoin(WorkForInventoryStaff, WorkForInventoryStaff.work_id == WorkForInventory.id)
            .where(
                or_(
                    WorkForInventory.created_by_user_id == current_user.id,
                    WorkForInventoryStaff.user_id == current_user.id,
                )
            )
            .distinct()
        )
    stmt = stmt.order_by(WorkForInventory.id.desc()).limit(100)
    rows = list(db.scalars(stmt).all())
    return templates.TemplateResponse(
        "work_products_list.html",
        _ctx(
            request,
            current_user=current_user,
            rows=rows,
            msg=msg,
            can_create=(current_user.role == UserRole.MASTER),
            work_mine_only=work_mine_only,
        ),
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
            _ctx(
                request,
                current_user=current_user,
                row=None,
                err="Работа не найдена.",
                mix_complexity_label="—",
            ),
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
                _ctx(
                    request,
                    current_user=current_user,
                    row=None,
                    err="Недостаточно прав для просмотра этой работы.",
                    mix_complexity_label="—",
                ),
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
    linked_sale_ids: list[int] = []
    if w.booking_id:
        linked_sale_ids = list(
            db.scalars(
                select(ProductSale.id)
                .where(
                    ProductSale.booking_id == int(w.booking_id),
                    ProductSale.is_voided.is_(False),
                )
                .order_by(ProductSale.id.asc())
            ).all()
        )
    return templates.TemplateResponse(
        "work_products_detail.html",
        _ctx(
            request,
            current_user=current_user,
            row=w,
            details=details,
            mix_complexity_label=ru_mix_complexity_label(details.get("mix_complexity")),
            err=None,
            saved=(request.query_params.get("msg") == "saved"),
            can_edit=can_edit,
            edit_allowed=edit_allowed,
            edit_block_msg=edit_block_msg,
            audit_rows=audit_rows,
            msg=void_msg,
            linked_sale_ids=linked_sale_ids,
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
@legacy_admin_router.post("/{work_id}/void")
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
    w.voided_at = utcnow_naive()
    w.voided_by_user_id = current_user.id
    w.updated_at = utcnow_naive()
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
@legacy_admin_router.post("/{work_id}/edit")
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

    prev_staff_sig = tuple(
        sorted(
            (
                (int(s.user_id), round(float(s.master_profit_amount or 0.0), 2))
                for s in (w.staff_rows or [])
            ),
            key=lambda x: x[0],
        )
    )
    prev_scope = getattr(w, "scope", None)

    def _p_float(name: str, default: float) -> float:
        try:
            return parse_float(form.get(name), default=default, field_name=name)
        except ValueError as e:
            raise ValueError(f"Некорректное число: {name}") from e

    def _p_int_opt(name: str) -> int | None:
        raw = (str(form.get(name) or "")).strip()
        if not raw:
            return None
        try:
            v = int(parse_float(raw, field_name=name))
        except ValueError as e:
            raise ValueError(f"Некорректное число: {name}") from e
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

    w.updated_at = utcnow_naive()
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
    new_staff_sig = tuple(
        sorted(
            ((int(s.user_id), round(float(s.master_profit_amount or 0.0), 2)) for s in staff_rows),
            key=lambda x: x[0],
        )
    )
    new_scope = getattr(w, "scope", None)
    if new_scope != prev_scope or new_staff_sig != prev_staff_sig:
        replace_work_accruals(db, w.id, staff_rows, current_user.id)
    db.commit()
    return RedirectResponse(url=f"/sales/work/{work_id}?msg=saved", status_code=303)

