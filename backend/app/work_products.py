"""
Работа с товарами: единая форма (в наличие / на заказ) + запись в work_for_inventory.
Этап 6.3.2: каркас, без детальных расчётов по видам.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.list_master_labels import work_list_master_labels
from app.list_search import parse_list_id_search
from app.payroll_fund import post_work_accruals, replace_work_accruals, storno_source_accruals
from starlette.datastructures import UploadFile

from app.auth import AuthUser, require_role
from app.client_payment import parse_client_payment_kind
from app.client_validation import format_created_by_label
from app.form_validation_log import log_user_validation_error
from app.db.models import (
    Client,
    ClientPaymentKind,
    Kit,
    KitAuthorStaff,
    KitBlanksCondition,
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
    WorkDraft,
    WorkForInventoryAuditLog,
    WorkForInventory,
    WorkForInventoryStaff,
    WorkKind,
    WorkRate,
    WorkScope,
)
from app.db.session import get_db
from app.user_roles import select_users_with_role, user_has_role
from app.kit_blank_stock_core import (
    composition_keys_intersection_catalog,
    ensure_blank_stock_from_composition,
    infer_kit_blanks_condition_from_totals,
    load_catalog_kit_maps,
    replace_blank_stock_for_kit,
)
from app.kit_composition import (
    KIT_INVENTORY_PIECE_EXCLUDE_KEYS,
    composition_json_from_lines,
    composition_json_from_totals,
)
from app.kit_composition_lines import (
    apply_global_used_discount,
    filter_nonempty,
    infer_blanks_condition,
    inventory_piece_count,
    inventory_totals_by_key,
    kit_by_staff_from_lines,
    lines_dicts_for_details,
    lines_from_form,
    lines_have_used,
    lines_to_json,
    lines_to_legacy_totals,
    stock_price_for_used_kit,
    stock_price_snapshot_for_used_kit,
    used_client_total_for_lines,
    client_price_for_lines,
)
from app.kit_inlay_visit import get_salon_cut_pct
from app.kit_inlay_visit import _materials_cost_and_snapshot
from app.work_products_compute import (
    compute_work_financials,
    kit_studio_profit_amount,
    split_profit_from_client_amount,
)
from app.forms_parse import parse_bool, parse_date_iso, parse_float, parse_int
from app.hourly_help import (
    apply_hourly_help_to_staff_profits,
    build_hourly_help_display_rows,
    hourly_help_rows_from_work_details,
    hourly_help_rows_to_json,
    parse_hourly_help_from_form,
    validate_hourly_help_rows,
)
from app.hourly_work import list_masters_for_hourly_work_form
from app.work_products_detail_view import (
    build_composition_table_view,
    catalog_product_name,
    rubber_type_label,
    work_profit_explanation,
)
from app.work_kit_edit import (
    apply_kit_work_edit,
    details_lines_to_initial_lines,
    read_staff_profits_from_form,
    replace_work_staff_rows,
    sync_work_kit_reserves_for_scope,
    work_kit_edit_template_extras,
)
from app.work_rate_keys import CUSTOM_ORDER_BONUS_MULTIPLIER, STUDIO_SHARE
from app.time_utils import utcnow_naive

_WORK_NEW_FP_KEYS = frozenset({
    "booking_id",
    "work_plan_id",
    "client_id",
    "performed_date",
    "scope",
    "kind",
    "order_booking_mode",
    "amount_from_client",
    "comment",
    "kanekalon_grams",
    "kudri_grams",
    "mix_source",
    "mix_complexity",
    "rubber_type",
    "rubber_family",
    "rubber_size",
    "rubber_attach_qty",
    "rubber_braids_qty",
    "other_product_id",
    "corr_trim_qty",
    "corr_hourly_hours",
    "corr_kit_description",
    "corr_kit_blanks_count",
    "corr_wash",
    "corr_steam",
    "corr_circle",
    "corr_add_kit_to_stock",
    "corr_kit_type_se",
    "corr_kit_type_de",
    "corr_kit_sku",
    "corr_kit_title",
    "corr_kit_used_discount_pct",
})
from app.visit_edit_policy import (
    edit_window_days,
    ensure_event_date_in_open_payroll_period,
    is_in_closed_payroll_period,
    require_closed_period_ack,
    user_may_edit_closed_payroll_period,
    within_edit_window,
)
from app.audit import diff_fields, write_audit_rows
from app.kit_crud import kit_key_excluded_from_client_price
from app.mix_rates import mix_rates_meta_json_dict
from app.ui_visit_display import ru_mix_complexity as ru_mix_complexity_label
from app.zakaz_blanks import kit_composition_catalog_items, kit_form_blank_defs
from app.webui import templates

router = APIRouter(prefix="/sales/work", tags=["work-products"])
_logger = logging.getLogger("livingbraiding.app")
# GET-алиас под старые закладки/ссылки (если где-то фигурировал /admin/sales/...):
#   /admin/sales/work/...  -> 308 -> /sales/work/...
legacy_admin_router = APIRouter(prefix="/admin/sales/work", tags=["work-products-legacy"])
_VIEW = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER))
_DETAIL = Depends(require_role(UserRole.MASTER, UserRole.HELPER, UserRole.ADMIN, UserRole.ADMIN_SUPER))
_MASTER = Depends(require_role(UserRole.MASTER))
_SUPER = Depends(require_role(UserRole.ADMIN_SUPER))
_EDIT = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER))


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
    current_user: AuthUser = _EDIT,
):
    return _redirect_admin_sales_work_to_canon(request, suffix=f"/{int(work_id)}/edit")


@legacy_admin_router.get("/{work_id}", response_class=HTMLResponse)
def work_detail_legacy_redirect(
    work_id: int,
    request: Request,
    current_user: AuthUser = _DETAIL,
):
    return _redirect_admin_sales_work_to_canon(request, suffix=f"/{int(work_id)}")


def _ctx(request: Request, current_user: AuthUser, **kwargs):
    return {"request": request, "current_user": current_user, **kwargs}


class _FormDictAdapter:
    """Адаптер dict формы для lines_from_form / getlist (kit_master_on)."""

    def __init__(self, data: dict[str, str]):
        self._data = data

    def keys(self):
        return self._data.keys()

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def getlist(self, key: str) -> list[str]:
        raw = self._data.get(key)
        if raw is None:
            return []
        if key == "kit_master_on" and "," in str(raw):
            return [p.strip() for p in str(raw).split(",") if p.strip()]
        return [str(raw)]


def _initial_kit_lines_from_fp(fp: dict[str, str], *, prefix: str = "kit_line") -> list[dict[str, Any]]:
    adapter = _FormDictAdapter(fp)
    lines = filter_nonempty(lines_from_form(adapter, prefix=prefix))
    return details_lines_to_initial_lines(lines_dicts_for_details(lines))


def _work_new_template_response(
    request: Request,
    *,
    current_user: AuthUser,
    db: Session,
    fp: dict[str, str],
    error: str | None = None,
    selected_client: Client | None = None,
    kit_master_on_ids: list[int] | None = None,
    status_code: int = 200,
    is_draft: bool = False,
    draft_id: int | None = None,
    draft_readonly: bool = False,
    lock_banner: dict[str, str] | None = None,
    draft_saved: bool = False,
):
    if selected_client is None:
        cid = (fp.get("client_id") or "").strip()
        try:
            cid_i = parse_int(cid, min=1, field_name="client_id") if cid else 0
        except ValueError:
            cid_i = 0
        if cid_i > 0:
            selected_client = db.get(Client, cid_i)
    masters = _list_masters_for_work_form(db)
    other_items = _other_items_for_work_form(db)
    work_price_meta = {
        "rubber": _zakaz_subcategory_services_map(db, "Хвосты/резинки"),
        "other": {str(x["id"]): x for x in other_items},
        "correction": _zakaz_subcategory_services_map(db, "Коррекция комплекта"),
        "customOrderBonus": _wr_float(db, CUSTOM_ORDER_BONUS_MULTIPLIER, 1.0),
        "mixRates": mix_rates_meta_json_dict(db),
        "salonCutPct": float(get_salon_cut_pct(db, current_user.id)),
    }
    kit_prefill = {k: v for k, v in fp.items() if k.startswith("kit_qty_")}
    kit_initial = _initial_kit_lines_from_fp(fp, prefix="kit_line")
    corr_initial = _initial_kit_lines_from_fp(fp, prefix="corr_kit_line")
    if kit_master_on_ids is None:
        from app.work_draft import kit_master_on_ids_from_fp

        kit_master_on_ids = kit_master_on_ids_from_fp(fp)
    return templates.TemplateResponse(
        "work_products_new.html",
        _ctx(
            request,
            current_user=current_user,
            error=error,
            fp=fp,
            selected_client=selected_client,
            masters=masters,
            masters_for_hourly_help=list_masters_for_hourly_work_form(db),
            kit_master_on_ids=kit_master_on_ids,
            kit_table_state_json=_kit_table_state_json(
                current_user, masters, kit_prefill, db, initial_lines=kit_initial
            ),
            corr_kit_table_state_json=_kit_table_state_json(
                current_user, masters, {}, db, initial_lines=corr_initial
            ),
            default_date=date.today().isoformat(),
            kinds=[{"value": k.value, "label": _kind_label(k)} for k in WorkKind],
            scopes=[
                {"value": s.value, "label": ("В наличие" if s == WorkScope.IN_STOCK else "На заказ")}
                for s in WorkScope
            ],
            kit_se_items=_kit_se_items(),
            kit_de_items=_kit_de_items(),
            rubber_types=[{"value": v, "label": l} for v, l in _rubber_type_items()],
            rubber_families=[{"value": v, "label": l} for v, l in _rubber_family_items()],
            rubber_sizes=[{"value": v, "label": l} for v, l in _rubber_size_items()],
            other_items=other_items,
            work_price_meta_json=json.dumps(work_price_meta, ensure_ascii=False),
            is_draft=is_draft,
            draft_id=draft_id,
            draft_readonly=draft_readonly,
            lock_banner=lock_banner,
            draft_saved=draft_saved,
        ),
        status_code=status_code,
    )


def _draft_lock_banner(db: Session, draft: WorkDraft) -> dict[str, str] | None:
    from app.ru_labels import ru_user_role

    if not draft.locked_by_user_id:
        return None
    holder = db.get(User, int(draft.locked_by_user_id))
    if not holder:
        return None
    return {
        "display_name": holder.display_name or holder.username,
        "role": ru_user_role(holder.role),
    }


def _work_edit_profit_meta_json(db: Session, work: WorkForInventory) -> str:
    details = _details_obj(work.details_json)
    mix_c = details.get("mix_complexity")
    return json.dumps(
        {
            "scope": work.scope.value if work.scope else WorkScope.CUSTOM_ORDER.value,
            "mixSource": work.mix_source.value if work.mix_source else MixSource.NO_MIX.value,
            "mixComplexity": str(mix_c) if mix_c else None,
            "mixRates": mix_rates_meta_json_dict(db),
            "mixCreatorUserId": int(work.created_by_user_id or 0),
        },
        ensure_ascii=False,
    )


def _work_edit_template_ctx(
    request: Request,
    current_user: AuthUser,
    work: WorkForInventory | None,
    *,
    err: str | None,
    db: Session,
) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "request": request,
        "current_user": current_user,
        "row": work,
        "err": err,
        "is_kit_work": False,
        "masters": [],
        "profit_master_uids": [],
        "staff_profit_by_uid": {},
        "scopes": [
            {"value": s.value, "label": ("В наличие" if s == WorkScope.IN_STOCK else "На заказ")}
            for s in WorkScope
        ],
    }
    if not work:
        return ctx
    closed_period_confirm_required = bool(
        is_in_closed_payroll_period(db, work.performed_date or work.created_at)
        and user_may_edit_closed_payroll_period(current_user)
    )
    ctx["closed_period_confirm_required"] = closed_period_confirm_required
    masters = _list_masters_for_work_form(db)
    staff_profit_by_uid = {
        int(s.user_id): float(s.master_profit_amount or 0.0) for s in (work.staff_rows or [])
    }
    profit_master_uids = list(staff_profit_by_uid.keys())
    ctx.update(
        masters=masters,
        staff_profit_by_uid=staff_profit_by_uid,
        profit_master_uids=profit_master_uids,
        use_per_master_profit=bool(profit_master_uids),
        selected_client=work.client,
    )
    if work.kind == WorkKind.KIT and work.created_kit_id:
        def _kit_state_builder(*, masters: list[User], initial_lines: list[dict[str, Any]]):
            return _kit_table_state_json(
                current_user,
                masters,
                {},
                db,
                initial_lines=initial_lines,
            )

        ctx.update(
            work_kit_edit_template_extras(
                db,
                work,
                kit_table_state_json_builder=_kit_state_builder,
                list_masters=_list_masters_for_work_form,
            )
        )
        ctx["work_edit_profit_meta_json"] = _work_edit_profit_meta_json(db, work)
        ctx["use_per_master_profit"] = True
        ctx["profit_master_uids"] = ctx.get("profit_master_uids") or ctx.get("kit_master_on_ids") or []
    return ctx


def _user_participates_in_work(work: WorkForInventory, user_id: int) -> bool:
    if int(work.created_by_user_id or 0) == int(user_id):
        return True
    return any(int(s.user_id) == int(user_id) for s in (work.staff_rows or []))


def _work_edit_window_block_message(days: int) -> str:
    return (
        f"Для админов и мастеров-участников редактирование доступно только в течение {days} дн. "
        f"с даты создания (параметр «Окно редактирования» в настройках студии). "
        f"Суперадмин может править, пока период ЗП не закрыт; техспец — всегда (в закрытом периоде с подтверждением)."
    )


def _work_edit_allowed(db: Session, work: WorkForInventory, user: AuthUser | None = None) -> tuple[bool, str]:
    """Как у визитов: окно дней — для ADMIN/MASTER; SUPER/TECHSPEC вне окна; закрытый период — техспец."""
    if getattr(work, "is_voided", False):
        return False, "Работа аннулирована — редактирование запрещено."
    event_at = work.performed_date or work.created_at
    if is_in_closed_payroll_period(db, event_at):
        if user is not None and user_may_edit_closed_payroll_period(user):
            return True, ""
        return False, (
            "Работа относится к закрытому периоду ЗП — редактирование запрещено "
            "(исключение: техспец с двойным подтверждением)."
        )
    if user is None:
        return False, "Недостаточно прав."
    if UserRole.ADMIN_SUPER in user.roles or UserRole.TECHSPEC in user.roles:
        return True, ""

    days = edit_window_days(db)
    inside = within_edit_window(work, days)

    if user.role == UserRole.ADMIN:
        if inside:
            return True, ""
        return False, _work_edit_window_block_message(days)

    if user.role == UserRole.MASTER:
        if not inside:
            return False, _work_edit_window_block_message(days)
        if not _user_participates_in_work(work, user.id):
            return False, "Редактировать работу может только создатель или мастер с долей в этой работе."
        return True, ""

    return False, "Недостаточно прав."


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
    *,
    initial_lines: list[dict[str, Any]] | None = None,
) -> str:
    kpg, kudpg = _material_prices_per_gram(db)
    return json.dumps(
        {
            "currentUserId": current_user.id,
            "masters": [{"id": u.id, "name": u.display_name} for u in masters],
            "seItems": [{"key": k, "label": lbl} for k, lbl in _kit_se_items()],
            "deItems": [{"key": k, "label": lbl} for k, lbl in _kit_de_items()],
            "blankCatalog": kit_composition_catalog_items(db),
            "initialLines": initial_lines or [],
            "prefill": kit_qty_prefill,
            "kitWorkPayByKey": _kit_work_pay_map_from_catalog(db),
            "kitPriceByKey": _kit_price_map_from_catalog(db),
            "salonCutPct": float(get_salon_cut_pct(db, current_user.id)),
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


def _rubber_family_items() -> list[tuple[str, str]]:
    return [
        ("TAIL_ELASTIC", "Хвост на резинке"),
        ("TAIL_CRAB", "Хвост на крабе"),
        ("TAIL_NET", "Хвост на сетке"),
        ("TAIL_BUN", "Хвост на бублике"),
        ("BRAIDS_ELASTIC", "Косы на резинке"),
    ]


def _rubber_size_items() -> list[tuple[str, str]]:
    return [
        ("MINI", "mini"),
        ("STANDARD", "standard"),
        ("MAX", "max"),
    ]


_RUBBER_SIZED_FAMILIES = frozenset({"TAIL_CRAB", "TAIL_NET", "TAIL_BUN"})
_VALID_RUBBER_TYPES = frozenset(k for k, _ in _rubber_type_items())


def _rubber_family_size_from_type(rubber_type: str) -> tuple[str, str]:
    rt = (rubber_type or "").strip()
    if rt in ("TAIL_ELASTIC", "BRAIDS_ELASTIC"):
        return rt, ""
    for prefix in ("TAIL_CRAB", "TAIL_NET", "TAIL_BUN"):
        if rt.startswith(prefix + "_"):
            return prefix, rt[len(prefix) + 1 :]
    return "", ""


def _enrich_fp_rubber(fp: dict[str, Any]) -> None:
    if (fp.get("rubber_family") or "").strip():
        return
    rt = (fp.get("rubber_type") or "").strip()
    if not rt:
        return
    fam, size = _rubber_family_size_from_type(rt)
    if fam:
        fp["rubber_family"] = fam
    if size:
        fp["rubber_size"] = size


def _resolve_rubber_type_from_form(form: Any) -> str:
    family = (_g_str(form, "rubber_family", "") or "").strip()
    if family:
        if family in _RUBBER_SIZED_FAMILIES:
            size = (_g_str(form, "rubber_size", "") or "").strip().upper()
            if size not in {k for k, _ in _rubber_size_items()}:
                raise ValueError("Для выбранного типа укажите размер: mini, standard или max.")
            return f"{family}_{size}"
        if family in ("TAIL_ELASTIC", "BRAIDS_ELASTIC"):
            return family
        raise ValueError("Для «Хвосты/резинки» выберите тип.")
    rubber_type = (_g_str(form, "rubber_type", "") or "").strip()
    if rubber_type in _VALID_RUBBER_TYPES:
        return rubber_type
    raise ValueError("Для «Хвосты/резинки» выберите тип.")


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


def _other_pricing_from_catalog(db: Session, catalog_product_id: int) -> tuple[float, float, float, bool, str | None]:
    """
    Возвращает: (master_pay, studio_pay, fixed_expense, is_per_unit, unit_label).
    Берём из прайса товаров (catalog_products): категория «Заказ» → подкатегория «Другое».
    """
    pid = int(catalog_product_id or 0)
    if pid <= 0:
        raise ValueError("Для «Другое» выберите товар.")
    row = db.get(CatalogProduct, pid)
    if not row or not row.is_active or row.category_name != "Заказ" or row.subcategory_name != "Другое":
        raise ValueError("Товар для «Другое» не найден в прайсе «Заказ → Другое».")
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


def _other_items_for_work_form(db: Session) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(CatalogProduct)
            .where(
                CatalogProduct.category_name == "Заказ",
                CatalogProduct.subcategory_name == "Другое",
                CatalogProduct.is_active.is_(True),
            )
            .order_by(CatalogProduct.sort_order.asc(), CatalogProduct.name.asc(), CatalogProduct.id.asc())
        ).all()
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            meta = json.loads(r.meta_json or "{}")
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        out.append(
            {
                "id": int(r.id),
                "name": str(r.name or ""),
                "price": float(r.price) if r.price is not None else None,
                "master_pay": float(meta.get("master_pay")) if meta.get("master_pay") is not None else None,
                "studio_pay": float(meta.get("studio_pay")) if meta.get("studio_pay") is not None else None,
                "fixed_expense": float(meta.get("fixed_expense")) if meta.get("fixed_expense") is not None else None,
                "is_per_unit": bool(meta.get("is_per_unit") or False),
                "unit_label": (str(meta.get("unit_label")) if meta.get("unit_label") else None),
            }
        )
    return out


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
        WorkKind.OTHER: "Другое",
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

    # 1.19: бонус за смешку в работе не включаем в себестоимость.
    # Оставляем source/complexity в записи, но денежный эффект отключён.

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


def _corr_kit_from_work_cost_snapshot_text(
    *,
    mat_cost: float,
    kanek: float,
    kudri: float,
    k_snap: float,
    ku_snap: float,
    correction_master_pay: float,
) -> str:
    """Себестоимость б/у комплекта с работы коррекции: материал + ЗП коррекции."""
    lines = ["Расчёт себестоимости комплекта (из работы «коррекция»):"]
    total = 0.0
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
    if correction_master_pay > 0:
        total += float(correction_master_pay)
        lines.append(f"ЗП за коррекцию — {_fmt_money(float(correction_master_pay))}")
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
    if (fp.get("booking_id") or "").strip():
        fp["order_booking_mode"] = "with_booking"
    elif not (fp.get("order_booking_mode") or "").strip():
        fp["order_booking_mode"] = "without_booking"
    _enrich_fp_rubber(fp)
    return _work_new_template_response(
        request,
        current_user=current_user,
        db=db,
        fp=fp,
        selected_client=selected_client,
        kit_master_on_ids=[],
    )


@router.get("/bookings/suggest")
def work_bookings_suggest(
    q: str = "",
    current_user: AuthUser = _MASTER,
    db: Session = Depends(get_db),
):
    from app.routes.bookings import suggest_bookings_for_work_order

    return JSONResponse({"bookings": suggest_bookings_for_work_order(db, q)})


@router.get("/draft/{draft_id}", response_class=HTMLResponse)
def work_draft_get(
    request: Request,
    draft_id: int,
    draft_saved: str | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    from app.ru_labels import ru_user_role
    from app.work_draft import (
        acquire_draft_lock,
        form_dict_from_json,
        kit_master_on_ids_from_fp,
        user_can_edit_draft,
        user_can_view_draft,
    )

    draft = db.scalar(
        select(WorkDraft)
        .where(WorkDraft.id == int(draft_id), WorkDraft.finalized_work_id.is_(None))
        .options(selectinload(WorkDraft.participants))
    )
    if not draft or not user_can_view_draft(current_user, draft, db):
        return RedirectResponse("/master/mywork", status_code=303)

    fp = form_dict_from_json(draft.form_json)
    if (fp.get("booking_id") or "").strip():
        fp["order_booking_mode"] = "with_booking"
    elif not (fp.get("order_booking_mode") or "").strip():
        fp["order_booking_mode"] = "without_booking"
    _enrich_fp_rubber(fp)
    lock_banner = None
    readonly = False
    if current_user.role in (UserRole.ADMIN, UserRole.ADMIN_SUPER):
        readonly = True
        lock_banner = _draft_lock_banner(db, draft)
    elif not user_can_edit_draft(current_user, draft, db):
        readonly = True
    else:
        lock = acquire_draft_lock(db, draft, current_user.id)
        readonly = lock.readonly
        if lock.lock_holder:
            lock_banner = {
                "display_name": lock.lock_holder.display_name or lock.lock_holder.username,
                "role": ru_user_role(lock.lock_holder.role),
            }
    db.commit()
    selected_client = db.get(Client, int(draft.client_id)) if draft.client_id else None
    return _work_new_template_response(
        request,
        current_user=current_user,
        db=db,
        fp=fp,
        selected_client=selected_client,
        kit_master_on_ids=kit_master_on_ids_from_fp(fp),
        is_draft=True,
        draft_id=int(draft.id),
        draft_readonly=readonly,
        lock_banner=lock_banner,
        draft_saved=draft_saved == "1",
    )


@router.post("/draft")
async def work_draft_create_post(
    request: Request,
    current_user: AuthUser = _MASTER,
    db: Session = Depends(get_db),
):
    from app.work_draft import collect_form_dict, save_work_draft

    form = await request.form()
    form_dict = collect_form_dict(form)
    try:
        draft = save_work_draft(db, None, form_dict, current_user.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        _enrich_fp_rubber(form_dict)
        return _work_new_template_response(
            request,
            current_user=current_user,
            db=db,
            fp=form_dict,
            error=str(exc),
            kit_master_on_ids=_read_kit_master_on_ids(form),
            status_code=400,
            is_draft=False,
            draft_id=None,
        )
    return RedirectResponse(f"/sales/work/draft/{draft.id}?draft_saved=1", status_code=303)


@router.post("/draft/{draft_id}")
async def work_draft_update_post(
    request: Request,
    draft_id: int,
    current_user: AuthUser = _MASTER,
    db: Session = Depends(get_db),
):
    from app.ru_labels import ru_user_role
    from app.work_draft import (
        acquire_draft_lock,
        collect_form_dict,
        save_work_draft,
        user_can_edit_draft,
    )

    form = await request.form()
    form_dict = collect_form_dict(form)
    draft = db.get(WorkDraft, int(draft_id))
    lock_banner = None
    if draft and draft.locked_by_user_id:
        holder = db.get(User, int(draft.locked_by_user_id))
        if holder:
            lock_banner = {
                "display_name": holder.display_name or holder.username,
                "role": ru_user_role(holder.role),
            }
    if not draft or not user_can_edit_draft(current_user, draft, db):
        return RedirectResponse("/master/mywork", status_code=303)
    lock = acquire_draft_lock(db, draft, current_user.id)
    if lock.readonly:
        db.rollback()
        if lock.lock_holder:
            lock_banner = {
                "display_name": lock.lock_holder.display_name or lock.lock_holder.username,
                "role": ru_user_role(lock.lock_holder.role),
            }
        _enrich_fp_rubber(form_dict)
        return _work_new_template_response(
            request,
            current_user=current_user,
            db=db,
            fp=form_dict,
            error="Черновик сейчас редактирует другой пользователь.",
            kit_master_on_ids=_read_kit_master_on_ids(form),
            status_code=400,
            is_draft=True,
            draft_id=int(draft_id),
            draft_readonly=True,
            lock_banner=lock_banner,
        )
    try:
        save_work_draft(db, int(draft_id), form_dict, current_user.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        _enrich_fp_rubber(form_dict)
        return _work_new_template_response(
            request,
            current_user=current_user,
            db=db,
            fp=form_dict,
            error=str(exc),
            kit_master_on_ids=_read_kit_master_on_ids(form),
            status_code=400,
            is_draft=True,
            draft_id=int(draft_id),
            draft_readonly=False,
            lock_banner=lock_banner,
        )
    return RedirectResponse(f"/sales/work/draft/{draft_id}?draft_saved=1", status_code=303)


@router.post("/draft/{draft_id}/unlock")
def work_draft_unlock_post(
    draft_id: int,
    current_user: AuthUser = _MASTER,
    db: Session = Depends(get_db),
):
    from app.work_draft import release_draft_lock

    draft = db.get(WorkDraft, int(draft_id))
    if draft:
        release_draft_lock(db, draft, current_user.id)
        db.commit()
    return RedirectResponse(f"/sales/work/draft/{draft_id}", status_code=303)


@router.post("/new")
@legacy_admin_router.post("/new")
async def work_new_post(
    request: Request,
    current_user: AuthUser = _MASTER,
    db: Session = Depends(get_db),
):
    form = await request.form()
    fp = {k: _g_str(form, k) for k in form.keys() if isinstance(k, str)}
    draft_id_raw = (_g_str(form, "draft_id", "") or "").strip()
    draft_id_val: int | None = None
    if draft_id_raw.isdigit():
        draft_id_val = int(draft_id_raw)
    try:
        if draft_id_val is not None:
            from app.work_draft import acquire_draft_lock, user_can_edit_draft

            draft = db.get(WorkDraft, int(draft_id_val))
            if not draft or not user_can_edit_draft(current_user, draft, db):
                raise ValueError("Черновик недоступен для сохранения работы.")
            lock = acquire_draft_lock(db, draft, current_user.id)
            if lock.readonly:
                raise ValueError("Черновик сейчас редактирует другой пользователь.")

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

        if _g_bool(form, "corr_add_kit_to_stock") and scope != WorkScope.IN_STOCK:
            raise ValueError("Комплект на склад можно добавить только в режиме «в наличие».")

        kind_raw = (_g_str(form, "kind", "") or "").strip()
        try:
            kind = WorkKind(kind_raw)
        except ValueError:
            raise ValueError("Выберите вид работы.")

        client_id: int | None = None
        amount_from_client: int | None = None
        client_payment_kind: ClientPaymentKind | None = None
        if scope == WorkScope.CUSTOM_ORDER:
            order_mode = (_g_str(form, "order_booking_mode", "") or "").strip().lower()
            bid_check = (_g_str(form, "booking_id", "") or "").strip()
            if order_mode == "with_booking" and not bid_check:
                raise ValueError("Выберите бронь или переключите на «без брони».")
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
            client_payment_kind = (
                parse_client_payment_kind(_g_str(form, "client_payment_kind"))
                if amount_from_client is not None
                else None
            )

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
        other_product_id = 0
        other_qty = 1
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
        composition_lines: list[Any] = []
        kit_bu_correction = False
        kit_catalog_client_price = 0.0
        corr_add_kit = False
        corr_kit_lines_saved: list[Any] = []
        corr_kit_discount_pct = 0
        corr_kit_sku = ""
        corr_kit_title = ""
        corr_kit_type_se = False
        corr_kit_type_de = False
        corr_kit_new_price = 0.0
        corr_kit_stock_price = 0.0
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
            composition_lines = filter_nonempty(lines_from_form(form))
            if not composition_lines:
                raise ValueError("Для комплекта укажите хотя бы одну строку состава (вид и количество).")
            kit_totals, kit_by_staff = kit_by_staff_from_lines(composition_lines)
            kit_pieces_total = sum(kit_totals.values())
            kit_pieces_inventory = inventory_piece_count(composition_lines)
            kit_bu_correction = _g_bool(form, "kit_bu_correction")
            if kit_bu_correction and not lines_have_used(composition_lines):
                raise ValueError("Коррекция Б/У доступна только при наличии б/у заготовок в составе.")
            details["kit"] = {
                "blank_type_se": kit_blank_type_se,
                "blank_type_de": kit_blank_type_de,
                "totals": kit_totals,
                "by_staff": {str(k): v for k, v in kit_by_staff.items()},
                "lines": lines_dicts_for_details(composition_lines),
                "bu_correction": kit_bu_correction,
            }
            kit_catalog_client_price, _kit_price_missing = client_price_for_lines(
                db, composition_lines, extra_costs_amount=float(extra_costs_amount)
            )
            details["kit"]["catalog_client_price"] = float(kit_catalog_client_price)
            if kit_bu_correction:
                corr_trim_qty = int(_g_float(form, "kit_corr_trim_qty", 0))
                corr_hourly_hours = max(0.0, _g_float(form, "kit_corr_hourly_hours", 0.0))
                corr_wash = _g_bool(form, "kit_corr_wash")
                corr_circle = _g_bool(form, "kit_corr_circle")
                corr_steam = _g_bool(form, "kit_corr_steam")
                corr_kit_description = (_g_str(form, "kit_corr_kit_description", "") or "").strip() or None
                raw_blanks = (_g_str(form, "kit_corr_kit_blanks_count", "") or "").strip()
                corr_kit_blanks_count: int | None = None
                if raw_blanks:
                    try:
                        corr_kit_blanks_count = int(
                            parse_float(raw_blanks, min=0.0, field_name="kit_corr_kit_blanks_count")
                        )
                    except ValueError:
                        raise ValueError("«Количество заготовок в комплекте» — целое число.")
                if corr_wash and corr_circle:
                    raise ValueError(
                        "Если выбрана «Стирка», то «Одевание на круг» выбирать нельзя (входит в стирку)."
                    )
                details["kit"]["bu_correction_details"] = {
                    "trim_qty": corr_trim_qty,
                    "hourly_hours": float(corr_hourly_hours),
                    "wash": corr_wash,
                    "circle": corr_circle,
                    "steam": corr_steam,
                    "kit_description": corr_kit_description,
                    "kit_blanks_count": corr_kit_blanks_count,
                }
            alloc = _alloc_equal_shares_for_masters(db, kit_staff_ids)
        elif kind == WorkKind.RUBBER:
            rubber_type = _resolve_rubber_type_from_form(form)
            if rubber_type not in _VALID_RUBBER_TYPES:
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
        elif kind == WorkKind.OTHER:
            other_raw = (_g_str(form, "other_product_id", "") or "").strip()
            try:
                other_product_id = parse_int(other_raw, min=1, field_name="other_product_id")
            except ValueError:
                raise ValueError("Для «Другое» выберите товар из прайса.")
            # validate presence in catalog; also gives correct error message
            _other_pricing_from_catalog(db, other_product_id)
            details["other"] = {"catalog_product_id": int(other_product_id)}
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

            corr_add_kit = _g_bool(form, "corr_add_kit_to_stock")
            if corr_add_kit:
                corr_kit_type_se = _g_bool(form, "corr_kit_type_se")
                corr_kit_type_de = _g_bool(form, "corr_kit_type_de")
                if not corr_kit_type_se and not corr_kit_type_de:
                    raise ValueError(
                        "Для комплекта на склад выберите тип заготовок: SE и/или DE."
                    )
                raw_corr_lines = filter_nonempty(lines_from_form(form, prefix="corr_kit_line"))
                if not raw_corr_lines:
                    raise ValueError(
                        "Для комплекта на склад укажите хотя бы одну строку состава (вид и количество)."
                    )
                pct_raw = (_g_str(form, "corr_kit_used_discount_pct", "") or "").strip()
                if not pct_raw:
                    raise ValueError("Укажите «Скидка за Б/У (%)» для комплекта на склад.")
                try:
                    corr_kit_discount_pct = int(parse_float(pct_raw, min=1.0, field_name="corr_kit_used_discount_pct"))
                except ValueError as exc:
                    raise ValueError("«Скидка за Б/У (%)» — целое число от 1 до 100.") from exc
                if corr_kit_discount_pct < 1 or corr_kit_discount_pct > 100:
                    raise ValueError("«Скидка за Б/У (%)» — от 1 до 100.")
                corr_kit_lines_saved = apply_global_used_discount(
                    raw_corr_lines, corr_kit_discount_pct
                )
                corr_kit_new_price, corr_kit_stock_price, missing_price = stock_price_for_used_kit(
                    db, raw_corr_lines, corr_kit_discount_pct
                )
                if missing_price:
                    miss = ", ".join(missing_price)
                    raise ValueError(
                        f"Не найдены цены в прайсе «Заказ → Заготовки поштучно» для: {miss}."
                    )
                corr_kit_sku = (_g_str(form, "corr_kit_sku", "") or "").strip()
                corr_kit_title = (_g_str(form, "corr_kit_title", "") or "").strip()
                if not corr_kit_sku:
                    raise ValueError("Для комплекта на склад укажите артикул.")
                if not corr_kit_title:
                    raise ValueError("Для комплекта на склад укажите название.")
                if db.scalar(select(Kit.id).where(Kit.sku == corr_kit_sku)):
                    raise ValueError("Комплект с таким артикулом уже есть — укажите другой.")
                details["corr_kit_to_stock"] = {
                    "sku": corr_kit_sku,
                    "title": corr_kit_title,
                    "blank_type_se": corr_kit_type_se,
                    "blank_type_de": corr_kit_type_de,
                    "used_discount_pct": corr_kit_discount_pct,
                    "new_price_total": float(corr_kit_new_price),
                    "stock_price_total": float(corr_kit_stock_price),
                    "lines": lines_dicts_for_details(corr_kit_lines_saved),
                }

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
            other_catalog_product_id=int(other_product_id or 0),
            other_qty=int(other_qty or 1),
            corr_trim_qty=corr_trim_qty,
            corr_hourly_hours=float(corr_hourly_hours),
            corr_hourly_avg=False,
            corr_wash=corr_wash,
            corr_circle=corr_circle,
            corr_steam=corr_steam,
            composition_lines=composition_lines if kind == WorkKind.KIT else None,
            kit_client_price=float(kit_catalog_client_price) if kind == WorkKind.KIT else None,
            amount_from_client=float(amount_from_client) if amount_from_client is not None else None,
        )
        staff_master_profit = dict(fin.staff_master_profit)
        corr_custom_studio_total: float | None = None
        if kind == WorkKind.KIT_CORRECTION and _g_bool(form, "corr_use_custom_amount"):
            raw_custom = (_g_str(form, "corr_custom_amount", "") or "").strip()
            if not raw_custom:
                raise ValueError("Укажите сумму с клиента для «Своей суммы».")
            try:
                custom_amt = int(parse_float(raw_custom, min=0.0, field_name="corr_custom_amount"))
            except ValueError:
                raise ValueError("Сумма с клиента должна быть числом.")
            if custom_amt <= 0:
                raise ValueError("Сумма с клиента должна быть больше нуля.")
            salon_pct = float(get_salon_cut_pct(db, current_user.id))
            profit, master_pay, studio_pay = split_profit_from_client_amount(
                custom_amt, float(fin.cost_total_amount), salon_pct
            )
            if profit < -0.01:
                raise ValueError("Сумма с клиента меньше себестоимости коррекции.")
            staff_master_profit = {current_user.id: float(master_pay)}
            corr_custom_studio_total = float(studio_pay)
            amount_from_client = custom_amt
            client_payment_kind = parse_client_payment_kind(_g_str(form, "corr_client_payment_kind"))
            if "correction" in details:
                details["correction"]["use_custom_amount"] = True
                details["correction"]["custom_amount"] = float(custom_amt)
        if kind == WorkKind.KIT and kit_bu_correction:
            bd = details["kit"].get("bu_correction_details") or {}
            corr_fin = compute_work_financials(
                db,
                kind=WorkKind.KIT_CORRECTION,
                scope=scope,
                alloc=[(current_user.id, 1.0)],
                current_user_id=current_user.id,
                mat_cost=0.0,
                kit_totals={},
                kit_staff_ids=[],
                kit_by_staff={},
                mix_source=None,
                mix_complexity=None,
                grams_total=0.0,
                rubber_type="",
                rubber_qty=0,
                other_catalog_product_id=0,
                other_qty=1,
                corr_trim_qty=int(bd.get("trim_qty") or 0),
                corr_hourly_hours=float(bd.get("hourly_hours") or 0.0),
                corr_hourly_avg=False,
                corr_wash=bool(bd.get("wash")),
                corr_circle=bool(bd.get("circle")),
                corr_steam=bool(bd.get("steam")),
            )
            used_total = used_client_total_for_lines(db, composition_lines)
            salon_pct = float(get_salon_cut_pct(db, current_user.id))
            max_corr_pay = max(0.0, used_total * (1.0 - salon_pct))
            corr_pay = float(corr_fin.master_total)
            if corr_pay > max_corr_pay + 0.01:
                raise ValueError(
                    "Сумма за коррекцию превышает допустимую сумму за Б/У заготовки."
                )
            staff_master_profit[current_user.id] = (
                float(staff_master_profit.get(current_user.id, 0.0)) + corr_pay
            )
        help_rows = parse_hourly_help_from_form(form)
        if kind == WorkKind.KIT and kit_staff_ids:
            participant_ids = {int(x) for x in kit_staff_ids}
        else:
            participant_ids = {int(current_user.id)}
        validate_hourly_help_rows(db, help_rows, participant_ids)
        staff_master_profit, helper_profits = apply_hourly_help_to_staff_profits(
            staff_master_profit,
            participant_ids,
            help_rows,
        )
        if help_rows:
            details["hourly_help"] = json.loads(hourly_help_rows_to_json(help_rows) or "[]")
        master_total = float(sum(staff_master_profit.values()) + sum(helper_profits.values()))
        cost_total_amount = fin.cost_total_amount
        extra_costs_amount = fin.extra_costs_amount
        if kind == WorkKind.KIT:
            studio_total = kit_studio_profit_amount(
                scope=scope,
                cost_total=float(cost_total_amount),
                master_total=master_total,
                amount_from_client=float(amount_from_client) if amount_from_client is not None else None,
            )
        elif corr_custom_studio_total is not None:
            studio_total = corr_custom_studio_total
        else:
            studio_total = fin.studio_total
        profit_total = master_total + studio_total
        studio_share = fin.studio_share_snapshot

        work = WorkForInventory(
            created_by_user_id=current_user.id,
            performed_date=performed_dt,
            kind=kind,
            scope=scope,
            client_id=client_id,
            amount_from_client=amount_from_client,
            client_payment_kind=client_payment_kind if amount_from_client is not None else None,
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
        wp_raw = (_g_str(form, "work_plan_id", "") or "").strip()
        wp_id_for_complete: int | None = None
        try:
            wp_i = parse_int(wp_raw, min=1, field_name="work_plan_id") if wp_raw else 0
        except ValueError:
            wp_i = 0
        if wp_i > 0:
            work.work_plan_id = wp_i
            wp_id_for_complete = wp_i
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
        alloc_uids = {int(uid) for uid, _ in alloc}
        for uid, amt in helper_profits.items():
            if int(uid) in alloc_uids:
                continue
            db.add(
                WorkForInventoryStaff(
                    work_id=work.id,
                    user_id=int(uid),
                    share=0.0,
                    master_profit_amount=float(amt),
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
            comp_json = lines_to_json(composition_lines) or composition_json_from_totals(kit_totals)
            stock_price_total, missing_price = client_price_for_lines(
                db, composition_lines, extra_costs_amount=float(extra_costs_amount)
            )
            if missing_price:
                miss = ", ".join(missing_price)
                raise ValueError(f"Не найдены цены в прайсе «Заказ → Заготовки поштучно» для: {miss}.")
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
                blanks_condition=infer_blanks_condition(composition_lines),
                description=None,
                is_active=True,
                pieces_total=kit_pieces_inventory,
                pieces_available=kit_pieces_inventory,
                blank_type_se=kit_blank_type_se,
                blank_type_de=kit_blank_type_de,
                weight_grams=None,
                length_cm=None,
                materials_text=None,
                color_text=None,
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
            ensure_blank_stock_from_composition(
                db, kit, quantities=inventory_totals_by_key(composition_lines)
            )
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

        if kind == WorkKind.KIT_CORRECTION and corr_add_kit:
            full_cost = float(cost_total_amount) + float(master_total)
            corr_kit_pieces = inventory_piece_count(corr_kit_lines_saved)
            comp_json = lines_to_json(corr_kit_lines_saved)
            stock_snap = stock_price_snapshot_for_used_kit(
                db,
                corr_kit_lines_saved,
                discount_pct=corr_kit_discount_pct,
            )
            cost_snap = _corr_kit_from_work_cost_snapshot_text(
                mat_cost=float(mat_cost),
                kanek=float(kanek),
                kudri=float(kudri),
                k_snap=float(k_snap or 0.0),
                ku_snap=float(ku_snap or 0.0),
                correction_master_pay=float(master_total),
            )
            kit_corr = Kit(
                sku=corr_kit_sku[:80],
                title=corr_kit_title[:200],
                blanks_condition=KitBlanksCondition.USED,
                description=None,
                is_active=True,
                pieces_total=corr_kit_pieces,
                pieces_available=corr_kit_pieces,
                blank_type_se=corr_kit_type_se,
                blank_type_de=corr_kit_type_de,
                weight_grams=None,
                length_cm=None,
                materials_text=None,
                color_text=None,
                notes=None,
                stock_price_total=float(corr_kit_stock_price),
                composition_json=comp_json,
                stock_price_snapshot_text=stock_snap,
                discount_percent=int(corr_kit_discount_pct),
                cost_total=full_cost,
                cost_snapshot_text=cost_snap,
                author_cost_total=None,
                created_at=utcnow_naive(),
                is_in_stock=True,
                is_archived=False,
            )
            db.add(kit_corr)
            db.flush()
            work.created_kit_id = kit_corr.id
            mu = db.get(User, current_user.id)
            if mu and mu.is_active and user_has_role(db, current_user.id, UserRole.MASTER):
                db.add(KitAuthorStaff(kit_id=kit_corr.id, user_id=current_user.id, sort_order=0))
            blank_qty = inventory_totals_by_key(corr_kit_lines_saved)
            comp_legacy = lines_to_legacy_totals(corr_kit_lines_saved)
            _, meta_map, _ = load_catalog_kit_maps(db)
            allowed = set(composition_keys_intersection_catalog(comp_legacy, meta_map)) if comp_legacy else set()
            if not allowed and comp_legacy:
                allowed = set(comp_legacy.keys())
            if not allowed:
                allowed = set(blank_qty.keys())
            if blank_qty:
                replace_blank_stock_for_kit(
                    db, kit_corr, quantities=blank_qty, allowed_keys=allowed
                )

        staff_saved = list(
            db.scalars(
                select(WorkForInventoryStaff).where(WorkForInventoryStaff.work_id == work.id)
            ).all()
        )
        db.flush()
        post_work_accruals(db, work.id, staff_saved, current_user.id)
        if draft_id_val is not None:
            from app.work_draft import link_finalized_work

            link_finalized_work(db, int(draft_id_val), int(work.id), current_user.id)
        db.commit()
        if bid_for_auto_complete is not None:
            from app.routes.bookings import try_auto_complete_booking

            try_auto_complete_booking(db, bid_for_auto_complete)
            db.commit()
        if wp_id_for_complete is not None:
            from app.work_plan import complete_work_plan_from_work

            complete_work_plan_from_work(db, wp_id_for_complete, work.id)
            db.commit()
        return RedirectResponse(url=f"/sales/work/{work.id}?msg=created", status_code=303)
    except ValueError as exc:
        log_user_validation_error(
            _logger,
            request=request,
            route="POST /sales/work/new",
            message=str(exc),
            form=form,
            user_id=current_user.id,
            username=current_user.username,
            context="work",
        )
        _enrich_fp_rubber(fp)
        return _work_new_template_response(
            request,
            current_user=current_user,
            db=db,
            fp=fp,
            error=str(exc),
            kit_master_on_ids=_read_kit_master_on_ids(form),
            status_code=400,
            is_draft=draft_id_val is not None,
            draft_id=draft_id_val,
            draft_readonly=False,
        )


@router.get("", response_class=HTMLResponse)
def work_list(
    request: Request,
    mine: str | None = Query(None),
    q: str | None = Query(None),
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

    list_search_q = (q or "").strip()
    search_id = parse_list_id_search(list_search_q)
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
    if search_id is not None:
        stmt = stmt.where(WorkForInventory.id == search_id)
    stmt = stmt.order_by(WorkForInventory.id.desc()).limit(100)
    rows = list(db.scalars(stmt).all())
    row_masters = work_list_master_labels(rows)
    return templates.TemplateResponse(
        "work_products_list.html",
        _ctx(
            request,
            current_user=current_user,
            rows=rows,
            row_masters=row_masters,
            msg=msg,
            can_create=(current_user.role == UserRole.MASTER),
            work_mine_only=work_mine_only,
            list_search_q=list_search_q,
            search_id=search_id,
        ),
    )


@router.get("/{work_id}", response_class=HTMLResponse)
def work_detail(
    request: Request,
    work_id: int,
    current_user: AuthUser = _DETAIL,
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
            selectinload(WorkForInventory.created_by_user),
            selectinload(WorkForInventory.staff_rows).selectinload(WorkForInventoryStaff.user),
        )
        .where(WorkForInventory.id == work_id)
    )
    if current_user.role in (UserRole.MASTER, UserRole.HELPER):
        allowed = (w.created_by_user_id == current_user.id) or any(
            s.user_id == current_user.id for s in (w.staff_rows or [])
        )
        if current_user.role == UserRole.HELPER:
            allowed = any(s.user_id == current_user.id for s in (w.staff_rows or []))
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
    can_edit = True
    edit_allowed, edit_block_msg = _work_edit_allowed(db, w, current_user)
    closed_period_confirm_required = bool(
        edit_allowed
        and is_in_closed_payroll_period(db, w.performed_date or w.created_at)
        and user_may_edit_closed_payroll_period(current_user)
    )
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
    consultation = None
    kit_detail = details.get("kit") if isinstance(details.get("kit"), dict) else None
    kit_composition_table = None
    if kit_detail and kit_detail.get("lines"):
        kit_composition_table = build_composition_table_view(
            db,
            lines=kit_detail.get("lines"),
            staff_rows=w.staff_rows or [],
        )
    corr_kit_stock = details.get("corr_kit_to_stock") if isinstance(details.get("corr_kit_to_stock"), dict) else None
    corr_kit_stock_table = None
    if corr_kit_stock and corr_kit_stock.get("lines"):
        corr_kit_stock_table = build_composition_table_view(
            db,
            lines=corr_kit_stock.get("lines"),
            staff_rows=w.staff_rows or [],
        )
    other_detail = details.get("other") if isinstance(details.get("other"), dict) else None
    other_product_label = None
    if other_detail:
        other_product_label = catalog_product_name(db, other_detail.get("catalog_product_id"))
    rubber_detail = details.get("rubber") if isinstance(details.get("rubber"), dict) else None
    rubber_label = rubber_type_label(rubber_detail.get("type") if rubber_detail else None)
    profit_explanation = work_profit_explanation(w, details)
    hourly_help_rows = build_hourly_help_display_rows(hourly_help_rows_from_work_details(details), db)
    if w.booking_id:
        from app.db.models import Booking

        booking_row = db.scalar(
            select(Booking)
            .where(Booking.id == int(w.booking_id))
            .options(selectinload(Booking.consultation))
        )
        if booking_row and booking_row.consultation:
            consultation = booking_row.consultation
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
            closed_period_confirm_required=closed_period_confirm_required,
            audit_rows=audit_rows,
            msg=void_msg,
            linked_sale_ids=linked_sale_ids,
            consultation=consultation,
            kit_detail=kit_detail,
            kit_composition_table=kit_composition_table,
            corr_kit_stock=corr_kit_stock,
            corr_kit_stock_table=corr_kit_stock_table,
            other_detail=other_detail,
            other_product_label=other_product_label,
            rubber_detail=rubber_detail,
            rubber_label=rubber_label,
            profit_explanation=profit_explanation,
            hourly_help_rows=hourly_help_rows,
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
    current_user: AuthUser = _EDIT,
    db: Session = Depends(get_db),
):
    w = db.scalar(
        select(WorkForInventory)
        .options(selectinload(WorkForInventory.staff_rows))
        .where(WorkForInventory.id == work_id)
    )
    if not w:
        return RedirectResponse(url="/sales/work?msg=not_found", status_code=303)
    if w.is_voided:
        return RedirectResponse(url=f"/sales/work/{work_id}?msg=already_voided", status_code=303)

    ok, _ = _work_edit_allowed(db, w, current_user)
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
    current_user: AuthUser = _EDIT,
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
            _work_edit_template_ctx(request, current_user, None, err="Работа не найдена.", db=db),
            status_code=404,
        )
    edit_allowed, edit_block_msg = _work_edit_allowed(db, w, current_user)
    if not edit_allowed:
        return templates.TemplateResponse(
            "work_products_edit.html",
            _work_edit_template_ctx(request, current_user, w, err=edit_block_msg, db=db),
            status_code=403,
        )
    return templates.TemplateResponse(
        "work_products_edit.html",
        _work_edit_template_ctx(request, current_user, w, err=None, db=db),
    )


@router.post("/{work_id}/edit")
@legacy_admin_router.post("/{work_id}/edit")
async def work_edit_save(
    request: Request,
    work_id: int,
    current_user: AuthUser = _EDIT,
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
    edit_allowed, edit_block_msg = _work_edit_allowed(db, w, current_user)
    if not edit_allowed:
        return templates.TemplateResponse(
            "work_products_edit.html",
            _work_edit_template_ctx(request, current_user, w, err=edit_block_msg, db=db),
            status_code=403,
        )
    form = await request.form()
    closed_needed = bool(
        is_in_closed_payroll_period(db, w.performed_date or w.created_at)
        and user_may_edit_closed_payroll_period(current_user)
    )
    try:
        require_closed_period_ack(needed=closed_needed, form_ack=form.get("closed_period_ack"))
    except ValueError as e:
        return templates.TemplateResponse(
            "work_products_edit.html",
            _work_edit_template_ctx(request, current_user, w, err=str(e), db=db),
            status_code=400,
        )

    prev_scope = getattr(w, "scope", None)
    prev_client_id = int(w.client_id) if w.client_id else None
    kit_edit_applied = False
    new_kit_staff_ids: list[int] = []
    kit_result = None

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
            scope=w.scope,
            client_id=w.client_id,
            amount_from_client=w.amount_from_client,
            client_payment_kind=w.client_payment_kind,
            comment=w.comment,
            kanekalon_grams=w.kanekalon_grams,
            kudri_grams=w.kudri_grams,
            materials_cost_total=w.materials_cost_total,
            extra_costs_amount=w.extra_costs_amount,
            cost_total_amount=w.cost_total_amount,
            master_profit_amount=w.master_profit_amount,
            studio_profit_amount=w.studio_profit_amount,
            profit_total_amount=w.profit_total_amount,
            details_json=w.details_json,
        )
        scope_raw = (_g_str(form, "scope", "") or "").strip()
        try:
            new_scope = WorkScope(scope_raw) if scope_raw else (w.scope or WorkScope.IN_STOCK)
        except ValueError as e:
            raise ValueError("Выберите режим: «в наличие» или «на заказ».") from e

        if new_scope == WorkScope.IN_STOCK:
            w.scope = WorkScope.IN_STOCK
            w.client_id = None
            w.amount_from_client = None
            w.client_payment_kind = None
        else:
            w.scope = WorkScope.CUSTOM_ORDER
            cid_raw = (_g_str(form, "client_id", "") or "").strip()
            if cid_raw:
                try:
                    cid = parse_int(cid_raw, min=1, field_name="client_id")
                except ValueError as e:
                    raise ValueError("Для режима «на заказ» выберите клиента.") from e
            else:
                cid = int(prev_client_id) if prev_client_id else 0
            if cid <= 0 or not db.get(Client, cid):
                raise ValueError("Для режима «на заказ» выберите клиента.")
            w.client_id = cid
            w.amount_from_client = _p_int_opt("amount_from_client")
            w.client_payment_kind = (
                parse_client_payment_kind(_g_str(form, "client_payment_kind"))
                if w.amount_from_client is not None
                else None
            )

        w.comment = (_g_str(form, "comment", "") or "").strip() or None

        w.kanekalon_grams = max(0.0, _p_float("kanekalon_grams", float(w.kanekalon_grams or 0.0)))
        w.kudri_grams = max(0.0, _p_float("kudri_grams", float(w.kudri_grams or 0.0)))
        w.materials_cost_total = max(0.0, _p_float("materials_cost_total", float(w.materials_cost_total or 0.0)))
        w.extra_costs_amount = max(0.0, _p_float("extra_costs_amount", float(w.extra_costs_amount or 0.0)))
        w.cost_total_amount = max(0.0, _p_float("cost_total_amount", float(w.cost_total_amount or 0.0)))

        w.studio_profit_amount = _p_float("studio_profit_amount", float(w.studio_profit_amount or 0.0))

        if w.kind == WorkKind.KIT and w.created_kit_id:
            kit_result = apply_kit_work_edit(
                db,
                w,
                form,
                extra_costs_amount=float(w.extra_costs_amount or 0.0),
                cost_total_amount=float(w.cost_total_amount or 0.0),
                alloc_equal_shares_for_masters=_alloc_equal_shares_for_masters,
                kit_stock_price_snapshot_text=_kit_stock_price_snapshot_text,
                kit_cost_snapshot_text=_kit_cost_snapshot_text,
            )
            w.master_profit_amount = float(kit_result.master_total)
            new_kit_staff_ids = list(kit_result.kit_staff_ids)
            kit_edit_applied = True
            w.studio_profit_amount = kit_studio_profit_amount(
                scope=w.scope,
                cost_total=float(w.cost_total_amount or 0.0),
                master_total=float(w.master_profit_amount or 0.0),
                amount_from_client=float(w.amount_from_client) if w.amount_from_client is not None else None,
            )
        elif w.staff_rows:
            active_uids = [int(s.user_id) for s in w.staff_rows]
            staff_profits = read_staff_profits_from_form(form, active_uids)
            w.master_profit_amount = float(sum(staff_profits.values()))
            for s in w.staff_rows:
                s.master_profit_amount = float(staff_profits.get(int(s.user_id), 0.0))
        else:
            w.master_profit_amount = max(0.0, _p_float("master_profit_amount", float(w.master_profit_amount or 0.0)))

        profit_raw = (_g_str(form, "profit_total_amount", "") or "").strip()
        if w.kind == WorkKind.KIT and w.created_kit_id:
            w.profit_total_amount = float(w.master_profit_amount or 0.0) + float(w.studio_profit_amount or 0.0)
        elif profit_raw:
            w.profit_total_amount = _p_float("profit_total_amount", float(w.profit_total_amount or 0.0))
        else:
            w.profit_total_amount = float(w.master_profit_amount or 0.0) + float(w.studio_profit_amount or 0.0)

        sync_work_kit_reserves_for_scope(
            db,
            w,
            prev_scope=prev_scope,
            prev_client_id=prev_client_id,
            actor_user_id=int(current_user.id),
        )
    except ValueError as exc:
        log_user_validation_error(
            _logger,
            request=request,
            route=f"POST /sales/work/{work_id}/edit",
            message=str(exc),
            form=form,
            user_id=current_user.id,
            username=current_user.username,
            context="work",
            extra={"work_id": work_id},
        )
        w_reload = db.scalar(
            select(WorkForInventory)
            .options(
                selectinload(WorkForInventory.client),
                selectinload(WorkForInventory.staff_rows).selectinload(WorkForInventoryStaff.user),
            )
            .where(WorkForInventory.id == work_id)
        )
        return templates.TemplateResponse(
            "work_products_edit.html",
            _work_edit_template_ctx(request, current_user, w_reload or w, err=str(exc), db=db),
            status_code=400,
        )

    audit_changes = diff_fields(
        before,
        w,
        (
            "scope",
            "client_id",
            "amount_from_client",
            "client_payment_kind",
            "comment",
            "kanekalon_grams",
            "kudri_grams",
            "materials_cost_total",
            "extra_costs_amount",
            "cost_total_amount",
            "master_profit_amount",
            "studio_profit_amount",
            "profit_total_amount",
            "details_json",
        ),
    )
    w.updated_at = utcnow_naive()
    w.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=WorkForInventoryAuditLog,
        entity_field="work_id",
        entity_id=w.id,
        changed_by_user_id=current_user.id,
        changes=audit_changes,
    )
    if kit_edit_applied and kit_result is not None:
        staff_rows = replace_work_staff_rows(
            db,
            w,
            new_kit_staff_ids,
            kit_result.staff_profits,
            _alloc_equal_shares_for_masters,
        )
    else:
        staff_rows = list(
            db.scalars(select(WorkForInventoryStaff).where(WorkForInventoryStaff.work_id == w.id)).all()
        )
    replace_work_accruals(db, w.id, staff_rows, current_user.id)
    db.commit()
    return RedirectResponse(url=f"/sales/work/{work_id}?msg=saved", status_code=303)

