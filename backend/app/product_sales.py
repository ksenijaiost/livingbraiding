"""
Этап 6.4: продажа товаров без услуги (материал/комплект/резинки/другое).
Доступ: мастер и админы.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import UploadFile

from app.auth import AuthUser, require_role
from app.client_payment import parse_client_payment_kind
from app.display_time import get_display_timezone
from app.ui_visit_display import ru_mix_complexity
from app.audit import diff_fields, write_audit_rows
from app.db.models import (
    Booking,
    BookingKind,
    Client,
    ClientPaymentKind,
    Kit,
    KitReserve,
    MixComplexity,
    MixSource,
    PayrollFundSourceKind,
    ProductSale,
    ProductSaleAuditLog,
    ProductSaleKind,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    UserRole,
    WorkForInventory,
    WorkKind,
)
from app.kit_blank_stock_core import (
    blank_stock_qty_map,
    build_usage_breakdown_keyed,
    decrement_blank_stock_keys,
    increment_blank_stock_keys,
    kit_inventory_is_keyed,
    max_take_by_key_for_client,
    parse_usage_breakdown_json,
    release_client_kit_reserves_into_free_pool,
    require_composition_stock_rows_or_scalar_ok,
    sync_kit_pieces_available_from_blank_lines,
)
from app.product_sale_material import (
    finalize_material_sale_fields,
    material_retail_has_pricing_path,
)
from app.payroll_fund import (
    compute_product_sale_studio_margin,
    post_product_sale_studio_accrual,
    replace_product_sale_studio_accrual,
    storno_source_accruals,
)
from app.db.session import get_db
from app.visit_edit_policy import (
    edit_window_days,
    ensure_event_date_in_open_payroll_period,
    is_in_closed_payroll_period,
    within_edit_window,
)
from app.ru_labels import ru_user_role
from app.forms_parse import parse_date_iso, parse_float, parse_int, parse_optional_float
from app.list_search import parse_list_id_search
from app.time_utils import utcnow_naive

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["ru_user_role"] = ru_user_role

router = APIRouter(prefix="/sales/products", tags=["product-sales"])
# GET-алиас под старые закладки/ссылки (если где-то фигурировал /admin/sales/...):
#   /admin/sales/products/...  -> 308 -> /sales/products/...
legacy_admin_router = APIRouter(prefix="/admin/sales/products", tags=["product-sales-legacy"])
_STAFF = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER))
_SUPER = Depends(require_role(UserRole.ADMIN_SUPER))


def _redirect_admin_sales_products_to_canon(request: Request, *, suffix: str = "") -> RedirectResponse:
    """Старые URL /admin/sales/products → канон /sales/products (GET, 308, query сохраняется)."""
    suf = (suffix or "").strip()
    if suf and not suf.startswith("/"):
        suf = f"/{suf}"
    new_path = f"/sales/products{suf}"
    return RedirectResponse(url=str(request.url.replace(path=new_path)), status_code=308)


@legacy_admin_router.get("", response_class=HTMLResponse)
def product_sales_list_legacy_redirect(
    request: Request,
    current_user: AuthUser = _STAFF,
):
    return _redirect_admin_sales_products_to_canon(request)


@legacy_admin_router.get("/new", response_class=HTMLResponse)
def product_sale_new_get_legacy_redirect(
    request: Request,
    current_user: AuthUser = _STAFF,
):
    return _redirect_admin_sales_products_to_canon(request, suffix="/new")


@legacy_admin_router.get("/{sale_id}/edit", response_class=HTMLResponse)
def product_sale_edit_get_legacy_redirect(
    sale_id: int,
    request: Request,
    current_user: AuthUser = _STAFF,
):
    return _redirect_admin_sales_products_to_canon(request, suffix=f"/{int(sale_id)}/edit")


@legacy_admin_router.get("/{sale_id}", response_class=HTMLResponse)
def product_sale_detail_legacy_redirect(
    sale_id: int,
    request: Request,
    current_user: AuthUser = _STAFF,
):
    return _redirect_admin_sales_products_to_canon(request, suffix=f"/{int(sale_id)}")


def _ctx(request: Request, current_user: AuthUser, **kwargs):
    return {"request": request, "current_user": current_user, **kwargs}


def _sale_edit_allowed(db: Session, sale: ProductSale) -> tuple[bool, str]:
    if sale.is_voided:
        return False, "Продажа аннулирована — редактирование запрещено."
    if is_in_closed_payroll_period(db, sale.created_at):
        return False, "Продажа относится к закрытому периоду ЗП — редактирование запрещено."
    days = edit_window_days(db)
    if not within_edit_window(sale, days):
        return False, (
            f"Редактирование доступно только в течение {days} дн. с даты создания "
            "(параметр «Окно редактирования» в настройках студии)."
        )
    return True, ""


def _apply_kit_delta(
    db: Session,
    kit_id: int,
    delta: int,
    *,
    breakdown: dict[str, int] | None = None,
) -> None:
    kit = db.get(Kit, kit_id)
    if not kit:
        raise ValueError("Комплект не найден.")
    if kit_inventory_is_keyed(db, int(kit_id)) and breakdown:
        s = sum(int(v) for v in breakdown.values() if int(v) > 0)
        if s != abs(int(delta)):
            raise ValueError("Внутренняя ошибка: разбивка списания не совпадает с количеством.")
        pos = {k: int(v) for k, v in breakdown.items() if int(v) > 0}
        if int(delta) < 0:
            decrement_blank_stock_keys(db, int(kit_id), pos)
        else:
            increment_blank_stock_keys(db, int(kit_id), pos)
        sync_kit_pieces_available_from_blank_lines(db, kit)
        return
    new_avail = int(kit.pieces_available + delta)
    if new_avail < 0:
        raise ValueError("Недостаточно заготовок в наличии для этой операции.")
    kit.pieces_available = new_avail


def _sum_reserved_for_client(db: Session, *, kit_id: int, client_id: int) -> int:
    if kit_id <= 0 or client_id <= 0:
        return 0
    total = db.scalar(
        select(func.coalesce(func.sum(KitReserve.pieces_reserved), 0)).where(
            KitReserve.kit_id == int(kit_id),
            KitReserve.reserved_for_client_id == int(client_id),
        )
    )
    return int(total or 0)


def _release_reserved_pieces_to_stock_for_client(
    db: Session,
    *,
    kit: Kit,
    client_id: int,
    pieces: int,
) -> int:
    """Освобождает резерв клиента в «наличие», чтобы затем списать по pieces_available.

    Возвращает фактически освобождённое количество (0..pieces).
    """
    n = int(pieces or 0)
    if n <= 0 or not kit or client_id <= 0:
        return 0
    rows = list(
        db.scalars(
            select(KitReserve)
            .where(
                KitReserve.kit_id == int(kit.id),
                KitReserve.reserved_for_client_id == int(client_id),
            )
            .order_by(KitReserve.id.asc())
        ).all()
    )
    left = n
    for r in rows:
        if left <= 0:
            break
        cur = int(r.pieces_reserved or 0)
        if cur <= 0:
            db.delete(r)
            continue
        take = min(cur, left)
        new_val = cur - take
        if new_val <= 0:
            db.delete(r)
        else:
            r.pieces_reserved = int(new_val)
        left -= take
    used = n - left
    if used > 0:
        kit.pieces_available = int(kit.pieces_available or 0) + int(used)
    return used


def _release_all_reserved_to_stock_for_client(
    db: Session,
    *,
    kit: Kit,
    client_id: int,
) -> int:
    """Снимает весь резерв клиента по комплекту и возвращает его на склад."""
    release_client_kit_reserves_into_free_pool(db, kit=kit, client_id=int(client_id))
    db.flush()
    return 0


def _ru_kind(k: ProductSaleKind) -> str:
    if k == ProductSaleKind.MATERIAL:
        return "Материал"
    if k == ProductSaleKind.KIT:
        return "Комплект"
    if k == ProductSaleKind.RUBBER:
        return "Хвост/резинка"
    if k == ProductSaleKind.OTHER:
        return "Другое"
    return k.value


def _prodazha_materiala_services(db: Session) -> list[Service]:
    return list(
        db.scalars(
            select(Service)
            .join(ServiceSubcategory, Service.subcategory_id == ServiceSubcategory.id)
            .join(ServiceCategory, ServiceSubcategory.category_id == ServiceCategory.id)
            .where(
                ServiceCategory.name == "Продажа материала",
                Service.is_active.is_(True),
            )
            .order_by(ServiceSubcategory.name.asc(), Service.name.asc())
            .options(selectinload(Service.subcategory))
        ).all()
    )


def _g_str(form: Any, name: str, default: str = "") -> str:
    v = form.get(name)
    if v is None or isinstance(v, UploadFile):
        return default
    if isinstance(v, (bytes, bytearray)):
        return v.decode().strip()
    return str(v).strip()


def _g_int(form: Any, name: str, default: int = 0) -> int:
    s = _g_str(form, name, "")
    if not s:
        return default
    try:
        return int(parse_float(s, field_name=name))
    except ValueError:
        return default


def _g_float(form: Any, name: str, default: float = 0.0) -> float:
    s = _g_str(form, name, "")
    if not s:
        return default
    try:
        return parse_float(s, field_name=name)
    except ValueError:
        return default


def _g_optional_float(form: Any, name: str) -> float | None:
    s = _g_str(form, name, "")
    if not s:
        return None
    try:
        return parse_optional_float(s, field_name=name)
    except ValueError:
        return None


def _parse_kit_breakdown_form(form: Any) -> dict[str, int] | None:
    raw = _g_str(form, "kit_breakdown_json")
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
            out[str(k)] = n
    return out or None


def _normalized_breakdown_dict(bd: Any) -> dict[str, int] | None:
    if not isinstance(bd, dict):
        return None
    out: dict[str, int] = {}
    for k, v in bd.items():
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out[str(k)] = n
    return out or None


def _product_sale_kit_lines_initial(
    db: Session,
    fp: dict[str, str],
    sale: ProductSale | None = None,
) -> list[dict[str, Any]]:
    """Строки UI «несколько комплектов» для new/edit (и репоста формы)."""
    raw = (fp.get("sale_kit_lines_json") or "").strip()
    if raw:
        try:
            arr = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            arr = None
        if isinstance(arr, list) and arr:
            out: list[dict[str, Any]] = []
            for item in arr:
                if not isinstance(item, dict):
                    continue
                try:
                    kid = int(item.get("kit_id") or 0)
                except (TypeError, ValueError):
                    kid = 0
                mode = str(item.get("mode") or "PIECES").strip().upper()
                if mode not in ("PIECES", "ALL"):
                    mode = "PIECES"
                try:
                    pi = int(item.get("pieces") or 0)
                except (TypeError, ValueError):
                    pi = 0
                if kid > 0:
                    out.append({"kit_id": kid, "mode": mode, "pieces": max(0, pi), "label": ""})
            if out:
                return _product_sale_kit_lines_attach_labels(db, out)
    if sale is not None and sale.kind == ProductSaleKind.KIT:
        kjs = getattr(sale, "kit_lines_json", None) or ""
        if str(kjs).strip():
            try:
                arr = json.loads(kjs)
            except (json.JSONDecodeError, TypeError):
                arr = None
            if isinstance(arr, list) and arr:
                out2: list[dict[str, Any]] = []
                for item in arr:
                    if not isinstance(item, dict):
                        continue
                    try:
                        kid = int(item.get("kit_id") or 0)
                        ps = int(item.get("pieces_sold") or 0)
                    except (TypeError, ValueError):
                        continue
                    if kid <= 0:
                        continue
                    out2.append({"kit_id": kid, "mode": "PIECES", "pieces": max(0, ps), "label": ""})
                if out2:
                    return _product_sale_kit_lines_attach_labels(db, out2)
        if sale.kit_id and sale.kit_pieces_sold:
            rows = [
                {
                    "kit_id": int(sale.kit_id),
                    "mode": "PIECES",
                    "pieces": int(sale.kit_pieces_sold or 0),
                    "label": "",
                }
            ]
            return _product_sale_kit_lines_attach_labels(db, rows)
    kid_s = (fp.get("kit_id") or "").strip()
    if kid_s.isdigit():
        mode = (fp.get("kit_mode") or "PIECES").strip().upper()
        if mode not in ("PIECES", "ALL"):
            mode = "PIECES"
        ps_s = (fp.get("kit_pieces_sold") or "").strip()
        try:
            pi = int(ps_s) if ps_s else 0
        except ValueError:
            pi = 0
        return _product_sale_kit_lines_attach_labels(
            db,
            [{"kit_id": int(kid_s), "mode": mode, "pieces": max(0, pi), "label": ""}],
        )
    return _product_sale_kit_lines_attach_labels(
        db, [{"kit_id": None, "mode": "PIECES", "pieces": 0, "label": ""}]
    )


def _product_sale_kit_lines_attach_labels(
    db: Session, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    for row in rows:
        kid = row.get("kit_id")
        if not kid:
            row["label"] = ""
            continue
        kobj = db.get(Kit, int(kid))
        row["label"] = (
            f"{kobj.sku} — {kobj.title}" if kobj else f"id {int(kid)}"
        )
    return rows


def _product_sale_kit_lines_for_detail(db: Session, sale: ProductSale) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    raw = getattr(sale, "kit_lines_json", None) or ""
    if str(raw).strip():
        try:
            arr = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            arr = None
        if isinstance(arr, list):
            for item in arr:
                if not isinstance(item, dict):
                    continue
                try:
                    kid = int(item.get("kit_id") or 0)
                    ps = int(item.get("pieces_sold") or 0)
                except (TypeError, ValueError):
                    continue
                if kid <= 0 or ps <= 0:
                    continue
                kit = db.get(Kit, kid)
                out.append({"kit": kit, "pieces_sold": ps})
    if not out and sale.kit_id and sale.kit_pieces_sold:
        kit = sale.kit or db.get(Kit, int(sale.kit_id))
        out.append({"kit": kit, "pieces_sold": int(sale.kit_pieces_sold)})
    return out


def _parse_sale_kit_lines_structured(form: Any) -> list[dict[str, Any]]:
    raw = (_g_str(form, "sale_kit_lines_json") or "").strip()
    if raw:
        try:
            arr = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Некорректный список комплектов в продаже.") from exc
        if not isinstance(arr, list):
            raise ValueError("Ожидается список комплектов.")
        lines: list[dict[str, Any]] = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            try:
                kid = int(item.get("kit_id") or 0)
            except (TypeError, ValueError):
                kid = 0
            if kid <= 0:
                continue
            mode = str(item.get("mode") or "PIECES").strip().upper()
            if mode not in ("PIECES", "ALL"):
                mode = "PIECES"
            try:
                pieces_in = int(item.get("pieces") or 0)
            except (TypeError, ValueError):
                pieces_in = 0
            lines.append(
                {
                    "kit_id": kid,
                    "mode": mode,
                    "pieces": max(0, pieces_in),
                    "breakdown": _normalized_breakdown_dict(item.get("breakdown")),
                }
            )
        if lines:
            return lines
    kid = 0
    try:
        kid = parse_int(_g_str(form, "kit_id"), min=1, field_name="kit_id")
    except ValueError:
        kid = 0
    if kid <= 0:
        return []
    mode = (_g_str(form, "kit_mode") or "PIECES").strip().upper()
    if mode not in ("PIECES", "ALL"):
        mode = "PIECES"
    pieces = _g_int(form, "kit_pieces_sold", 0) if mode == "PIECES" else 0
    return [
        {
            "kit_id": kid,
            "mode": mode,
            "pieces": max(0, pieces),
            "breakdown": _parse_kit_breakdown_form(form),
        }
    ]


def _apply_parsed_kit_sale_lines(
    db: Session,
    client: Client,
    parsed_lines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    saved: list[dict[str, Any]] = []
    for pl in parsed_lines:
        kid = int(pl["kit_id"])
        kit = db.get(Kit, kid)
        if not kit:
            raise ValueError("Комплект не найден.")
        if kit.stock_price_total is None or float(kit.stock_price_total) <= 0:
            raise ValueError(
                "У этого комплекта не задана цена продажи — укажите цену в карточке комплекта (администратор)."
            )
        mode = str(pl.get("mode") or "PIECES").upper()
        if mode not in ("PIECES", "ALL"):
            mode = "PIECES"
        _release_all_reserved_to_stock_for_client(db, kit=kit, client_id=client.id)
        require_composition_stock_rows_or_scalar_ok(db, kit)
        avail = int(kit.pieces_available or 0)
        if kit_inventory_is_keyed(db, kit.id):
            sm = blank_stock_qty_map(db, kit.id)
            max_by = max_take_by_key_for_client(db, kit=kit, client_id=int(client.id), stock_map=sm)
            if sum(max_by.values()) <= 0:
                raise ValueError("Нет доступных заготовок для продажи по этому комплекту.")
            usage = pl.get("breakdown")
            use_entire = mode == "ALL"
            blanks_used = int(pl.get("pieces") or 0) if mode == "PIECES" else 0
            bd = build_usage_breakdown_keyed(
                use_entire=use_entire,
                blanks_used=blanks_used,
                usage_by_key=usage,
                max_by_key=max_by,
            )
            ntot = sum(int(v) for v in bd.values())
            _apply_kit_delta(db, kit.id, -int(ntot), breakdown=bd)
            saved.append({"kit_id": int(kit.id), "pieces_sold": int(ntot), "breakdown": bd})
        else:
            pieces_to_sell = avail
            if mode == "PIECES":
                pieces_to_sell = int(pl.get("pieces") or 0)
                if pieces_to_sell <= 0:
                    raise ValueError("Укажите количество заготовок больше 0.")
            else:
                pieces_to_sell = avail
            if pieces_to_sell <= 0:
                raise ValueError("Нет доступных заготовок для продажи по этому комплекту.")
            if pieces_to_sell > avail:
                raise ValueError("Нельзя продать больше заготовок, чем есть в наличии.")
            _apply_kit_delta(db, kit.id, -int(pieces_to_sell))
            saved.append({"kit_id": int(kit.id), "pieces_sold": int(pieces_to_sell), "breakdown": None})
    if not saved:
        raise ValueError("Выберите хотя бы один комплект из наличия.")
    return saved


def _sale_kit_line_tuples_from_sale(sale: ProductSale) -> list[tuple[int, int, dict[str, int] | None]]:
    raw = getattr(sale, "kit_lines_json", None) or ""
    if str(raw).strip():
        try:
            arr = json.loads(raw)
        except Exception:
            arr = []
        if isinstance(arr, list) and arr:
            out: list[tuple[int, int, dict[str, int] | None]] = []
            for item in arr:
                if not isinstance(item, dict):
                    continue
                try:
                    kid = int(item.get("kit_id") or 0)
                    ps = int(item.get("pieces_sold") or 0)
                except (TypeError, ValueError):
                    continue
                if kid <= 0 or ps <= 0:
                    continue
                bd = item.get("breakdown")
                bdm = _normalized_breakdown_dict(bd) if bd is not None else None
                out.append((kid, ps, bdm))
            if out:
                return out
    if sale.kit_id and sale.kit_pieces_sold:
        return [
            (
                int(sale.kit_id),
                int(sale.kit_pieces_sold),
                parse_usage_breakdown_json(getattr(sale, "kit_breakdown_json", None)),
            )
        ]
    return []


def _revert_kit_sale_stock(db: Session, sale: ProductSale) -> None:
    for kid, ps, bd in _sale_kit_line_tuples_from_sale(sale):
        _apply_kit_delta(db, kid, int(ps), breakdown=bd)


def _apply_material_from_form(
    db: Session,
    sale: ProductSale,
    form: Any,
    allowed_material_ids: set[int],
    current_user: AuthUser,
) -> None:
    sid_raw = _g_str(form, "material_service_id")
    try:
        service_id = parse_int(sid_raw, min=1, field_name="material_service_id")
    except ValueError:
        service_id = None
    if service_id and service_id not in allowed_material_ids:
        raise ValueError("Недопустимая услуга «Продажа материала».")
    sale.material_service_id = service_id
    sale.material_description = (_g_str(form, "material_description") or "").strip() or None

    sale.material_kanekalon_grams = None
    sale.material_kudri_grams = None
    sale.material_manual_cost = None
    sale.material_mix_standalone_grams = None
    sale.material_mix_source = None
    sale.material_mix_complexity = None
    sale.material_grams = None

    svc = db.get(Service, service_id) if service_id else None
    if svc is None:
        grams = _g_float(form, "material_grams", 0.0)
        if grams <= 0:
            raise ValueError("Для «Материал» укажите количество грамм больше 0.")
        sale.material_grams = float(grams)
        return

    if svc.retail_material_kanekalon or svc.retail_material_kudri:
        gk = _g_float(form, "material_kanekalon_grams", 0.0) if svc.retail_material_kanekalon else 0.0
        gku = _g_float(form, "material_kudri_grams", 0.0) if svc.retail_material_kudri else 0.0
        if gk + gku <= 0:
            raise ValueError("Укажите граммы материала (канекалон и/или кудри).")
        if svc.retail_material_kanekalon:
            sale.material_kanekalon_grams = float(gk)
        if svc.retail_material_kudri:
            sale.material_kudri_grams = float(gku)

    sale.material_manual_cost = _g_optional_float(form, "material_manual_cost")

    if svc.retail_material_mix:
        if not (svc.retail_material_kanekalon or svc.retail_material_kudri):
            sg = _g_float(form, "material_mix_standalone_grams", 0.0)
            if sg <= 0:
                raise ValueError("Укажите граммы для смешки.")
            sale.material_mix_standalone_grams = float(sg)
        ms = (_g_str(form, "material_mix_source") or "").strip().upper()
        if current_user.role == UserRole.MASTER:
            if ms not in ("FROM_STOCK", "SELF_MIXED"):
                raise ValueError("Укажите источник смешки: из наличия или сама мешала.")
            sale.material_mix_source = (
                MixSource.FROM_STOCK if ms == "FROM_STOCK" else MixSource.SELF_MIXED
            )
        else:
            sale.material_mix_source = MixSource.FROM_STOCK
        mc = (_g_str(form, "material_mix_complexity") or "").strip().upper()
        mc = {"SIMPLE": "STANDARD", "MEDIUM": "KANEK", "HARD": "THERMO"}.get(mc, mc)
        try:
            sale.material_mix_complexity = MixComplexity(mc) if mc else None
        except ValueError:
            sale.material_mix_complexity = None
        if sale.material_mix_complexity is None:
            raise ValueError("Укажите сложность смешки.")


def _booking_amount_hint_for_prefill(b: Booking) -> str:
    if b.deposit_amount is not None and int(b.deposit_amount) > 0:
        return str(int(b.deposit_amount))
    raw = (b.quoted_price_text or "").strip()
    if not raw:
        return ""
    m = re.search(r"(\d[\d\s]{2,})", raw)
    if not m:
        return ""
    digits = re.sub(r"\D", "", m.group(1))
    return digits if digits else ""


def _merge_product_sale_fp_from_booking(db: Session, b: Booking, fp: dict[str, str]) -> None:
    if b.kind != BookingKind.PRODUCT_SALE:
        return
    fp["existing_client_id"] = str(b.client_id)
    pk = (b.planned_product_kind or "MATERIAL").strip().upper()
    if pk in ("MATERIAL", "KIT", "RUBBER", "OTHER"):
        fp["kind"] = pk
    tz = get_display_timezone(db)
    if b.planned_date:
        utc_dt = (
            b.planned_date.replace(tzinfo=ZoneInfo("UTC"))
            if b.planned_date.tzinfo is None
            else b.planned_date.astimezone(ZoneInfo("UTC"))
        )
        local_dt = utc_dt.astimezone(ZoneInfo(tz)).replace(tzinfo=None)
        fp["performed_date"] = local_dt.date().isoformat()
    hint = _booking_amount_hint_for_prefill(b)
    if hint:
        fp["amount_from_client"] = hint
    details: dict[str, Any] = {}
    try:
        raw = json.loads(b.details_json or "{}")
        if isinstance(raw, dict):
            details = raw
    except Exception:
        pass
    k = (fp.get("kind") or "").upper()
    if k == "RUBBER":
        desc = str(details.get("sale_rubber_desc") or "").strip()
        if desc:
            fp["rubber_description"] = desc
        rv = str(details.get("sale_rubber_price_override") or "").strip()
        if rv:
            try:
                rv_i = parse_int(rv, min=0, field_name="sale_rubber_price_override")
            except ValueError:
                rv_i = None
            if rv_i is not None:
                fp["rubber_price_override"] = str(rv_i)
    elif k == "KIT":
        sm = str(details.get("sale_kit_mode") or "").strip().upper()
        if sm == "IN_STOCK":
            sk = str(details.get("sale_stock_kit_id") or "").strip()
            try:
                sk_i = parse_int(sk, min=1, field_name="sale_stock_kit_id") if sk else 0
            except ValueError:
                sk_i = 0
            if sk_i > 0:
                fp["kit_id"] = str(sk_i)
            sp = str(details.get("sale_stock_kit_pieces") or "").strip()
            try:
                sp_i = parse_int(sp, min=0, field_name="sale_stock_kit_pieces") if sp else 0
            except ValueError:
                sp_i = 0
            if sp_i > 0:
                fp["kit_pieces_sold"] = str(sp_i)
    elif k == "OTHER":
        od = str(details.get("sale_rubber_desc") or "").strip()
        if od:
            fp["other_description"] = od
    elif k == "MATERIAL":
        md = str(details.get("sale_rubber_desc") or "").strip()
        if md:
            fp["material_description"] = md

    # Комплект из «работы с товарами» по этой брони (заказ + резерв и т.д.): в details брони kit_id может отсутствовать.
    if (fp.get("kind") or "").strip().upper() == "KIT":
        kid_raw = (fp.get("kit_id") or "").strip()
        try:
            kid_i = parse_int(kid_raw, min=1, field_name="kit_id") if kid_raw else 0
        except ValueError:
            kid_i = 0
        if kid_i <= 0:
            work = db.scalar(
                select(WorkForInventory)
                .where(
                    WorkForInventory.booking_id == b.id,
                    WorkForInventory.is_voided.is_(False),
                    WorkForInventory.kind == WorkKind.KIT,
                    WorkForInventory.created_kit_id.isnot(None),
                )
                .order_by(WorkForInventory.id.desc())
                .limit(1)
            )
            if work and work.created_kit_id:
                fp["kit_id"] = str(int(work.created_kit_id))
        kid_raw = (fp.get("kit_id") or "").strip()
        try:
            rid = parse_int(kid_raw, min=1, field_name="kit_id") if kid_raw else 0
        except ValueError:
            rid = 0
        if rid > 0 and not (fp.get("kit_pieces_sold") or "").strip().isdigit():
            total_r = db.scalar(
                select(func.coalesce(func.sum(KitReserve.pieces_reserved), 0)).where(
                    KitReserve.kit_id == rid,
                    KitReserve.reserved_for_client_id == b.client_id,
                )
            )
            total_r = int(total_r or 0)
            if total_r > 0:
                fp["kit_pieces_sold"] = str(total_r)
            else:
                kit_row = db.get(Kit, rid)
                if kit_row and int(kit_row.pieces_total or 0) > 0:
                    fp["kit_pieces_sold"] = str(int(kit_row.pieces_total))


def _material_services_meta_json(services: list[Service]) -> str:
    d: dict[str, dict[str, bool]] = {}
    for s in services:
        d[str(s.id)] = {
            "k": bool(s.retail_material_kanekalon),
            "ku": bool(s.retail_material_kudri),
            "m": bool(s.retail_material_mix),
        }
    return json.dumps(d, ensure_ascii=False)


def _render_new(
    request: Request,
    current_user: AuthUser,
    db: Session,
    *,
    error: str | None = None,
    fp: dict | None = None,
):
    fp = fp or {}
    material_services = _prodazha_materiala_services(db)
    selected_client = None
    eid = (fp.get("existing_client_id") or "").strip()
    try:
        eid_i = parse_int(eid, min=1, field_name="existing_client_id") if eid else 0
    except ValueError:
        eid_i = 0
    if eid_i > 0:
        selected_client = db.get(Client, eid_i)
    sale_kit_lines_initial = _product_sale_kit_lines_initial(db, fp, sale=None)
    default_date = (fp.get("performed_date") or "").strip() or date.today().isoformat()
    return templates.TemplateResponse(
        "product_sale_new.html",
        _ctx(
            request,
            current_user=current_user,
            error=error,
            fp=fp,
            selected_client=selected_client,
            sale_kit_lines_initial=sale_kit_lines_initial,
            default_date=default_date,
            material_services=material_services,
            material_services_meta_json=_material_services_meta_json(material_services),
        ),
        status_code=400 if error else 200,
    )


@router.get("", response_class=HTMLResponse)
def product_sales_list(
    request: Request,
    q: str | None = Query(None),
    current_user: AuthUser = _STAFF,
    db: Session = Depends(get_db),
):
    msg = request.query_params.get("msg")
    list_search_q = (q or "").strip()
    search_id = parse_list_id_search(list_search_q)
    stmt = (
        select(ProductSale)
        .options(
            selectinload(ProductSale.client),
            selectinload(ProductSale.created_by_user),
            selectinload(ProductSale.voided_by_user),
            selectinload(ProductSale.material_service).selectinload(Service.subcategory),
            selectinload(ProductSale.kit),
        )
        .order_by(ProductSale.created_at.desc())
        .limit(200)
    )
    if search_id is not None:
        stmt = stmt.where(ProductSale.id == search_id)
    rows = list(db.scalars(stmt).all())
    return templates.TemplateResponse(
        "product_sales_list.html",
        _ctx(
            request,
            current_user=current_user,
            rows=rows,
            msg=msg,
            list_search_q=list_search_q,
            search_id=search_id,
        ),
    )


@router.get("/new", response_class=HTMLResponse)
def product_sale_new_get(
    request: Request,
    current_user: AuthUser = _STAFF,
    db: Session = Depends(get_db),
):
    fp: dict[str, str] = {}
    cid = str(request.query_params.get("client_id") or "").strip()
    try:
        cid_i = parse_int(cid, min=1, field_name="client_id") if cid else 0
    except ValueError:
        cid_i = 0
    if cid_i > 0:
        fp["existing_client_id"] = str(cid_i)
    bid = str(request.query_params.get("booking_id") or "").strip()
    try:
        bid_i = parse_int(bid, min=1, field_name="booking_id") if bid else 0
    except ValueError:
        bid_i = 0
    if bid_i > 0:
        fp["booking_id"] = str(bid_i)
        b = db.scalar(select(Booking).where(Booking.id == bid_i))
        if b:
            _merge_product_sale_fp_from_booking(db, b, fp)
    return _render_new(request, current_user, db, fp=fp)


@router.get("/{sale_id}", response_class=HTMLResponse)
def product_sale_detail(
    sale_id: int,
    request: Request,
    current_user: AuthUser = _STAFF,
    db: Session = Depends(get_db),
):
    sale = db.scalar(
        select(ProductSale)
        .where(ProductSale.id == sale_id)
        .options(
            selectinload(ProductSale.client),
            selectinload(ProductSale.created_by_user),
            selectinload(ProductSale.voided_by_user),
            selectinload(ProductSale.material_service).selectinload(Service.subcategory),
            selectinload(ProductSale.material_mix_bonus_user),
            selectinload(ProductSale.kit),
        )
    )
    if not sale:
        return templates.TemplateResponse(
            "product_sale_detail.html",
            _ctx(
                request,
                current_user=current_user,
                sale=None,
                error="Продажа не найдена.",
                can_edit=False,
                edit_allowed=False,
                edit_block_msg="",
                ru_kind="",
                material_mix_complexity_ru="—",
                kit_sale_lines_detail=[],
                linked_work_ids=[],
            ),
            status_code=404,
        )
    edit_allowed, edit_block_msg = _sale_edit_allowed(db, sale)
    can_edit = current_user.role == UserRole.ADMIN_SUPER
    audit_rows = list(
        db.scalars(
            select(ProductSaleAuditLog)
            .options(selectinload(ProductSaleAuditLog.changed_by_user))
            .where(ProductSaleAuditLog.sale_id == sale_id)
            .order_by(ProductSaleAuditLog.changed_at.desc(), ProductSaleAuditLog.id.desc())
            .limit(200)
        ).all()
    )
    linked_work_ids: list[int] = []
    consultation = None
    if sale.booking_id:
        from app.db.models import Booking

        booking_row = db.scalar(
            select(Booking)
            .where(Booking.id == int(sale.booking_id))
            .options(selectinload(Booking.consultation))
        )
        if booking_row and booking_row.consultation:
            consultation = booking_row.consultation
        linked_work_ids = list(
            db.scalars(
                select(WorkForInventory.id)
                .where(
                    WorkForInventory.booking_id == int(sale.booking_id),
                    WorkForInventory.is_voided.is_(False),
                )
                .order_by(WorkForInventory.id.asc())
            ).all()
        )
    return templates.TemplateResponse(
        "product_sale_detail.html",
        _ctx(
            request,
            current_user=current_user,
            sale=sale,
            msg=request.query_params.get("msg"),
            error=None,
            audit_rows=audit_rows,
            can_edit=can_edit,
            edit_allowed=edit_allowed,
            edit_block_msg=edit_block_msg,
            ru_kind=_ru_kind(sale.kind),
            material_mix_complexity_ru=ru_mix_complexity(
                getattr(sale, "material_mix_complexity", None)
            ),
            linked_work_ids=linked_work_ids,
            consultation=consultation,
            kit_sale_lines_detail=_product_sale_kit_lines_for_detail(db, sale)
            if sale.kind == ProductSaleKind.KIT
            else [],
        ),
    )


@router.get("/{sale_id}/edit", response_class=HTMLResponse)
def product_sale_edit_form(
    sale_id: int,
    request: Request,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    sale = db.scalar(
        select(ProductSale)
        .where(ProductSale.id == sale_id)
        .options(
            selectinload(ProductSale.client),
            selectinload(ProductSale.material_service).selectinload(Service.subcategory),
            selectinload(ProductSale.kit),
        )
    )
    if not sale:
        sk_init_404 = _product_sale_kit_lines_initial(db, {}, sale=None)
        return templates.TemplateResponse(
            "product_sale_edit.html",
            _ctx(
                request,
                current_user=current_user,
                sale=None,
                error="Продажа не найдена.",
                fp={},
                selected_client=None,
                default_date=date.today().isoformat(),
                material_services=_prodazha_materiala_services(db),
                material_services_meta_json="{}",
                sale_kit_lines_initial=sk_init_404,
            ),
            status_code=404,
        )
    ok, msg = _sale_edit_allowed(db, sale)
    if not ok:
        ms403 = _prodazha_materiala_services(db)
        sk_init_403 = _product_sale_kit_lines_initial(db, {}, sale=sale)
        return templates.TemplateResponse(
            "product_sale_edit.html",
            _ctx(
                request,
                current_user=current_user,
                sale=sale,
                error=msg,
                fp={},
                selected_client=sale.client,
                default_date=sale.performed_date.date().isoformat(),
                material_services=ms403,
                material_services_meta_json=_material_services_meta_json(ms403),
                sale_kit_lines_initial=sk_init_403,
            ),
            status_code=403,
        )
    ms = _prodazha_materiala_services(db)
    err_qp = request.query_params.get("err")
    fp = {
        "existing_client_id": str(sale.client_id),
        "performed_date": sale.performed_date.date().isoformat(),
        "amount_from_client": str(sale.amount_from_client),
        "client_payment_kind": sale.client_payment_kind.value if sale.client_payment_kind else ClientPaymentKind.CASH.value,
        "kind": sale.kind.value,
        "material_service_id": str(sale.material_service_id or ""),
        "material_grams": "" if sale.material_grams is None else str(sale.material_grams),
        "material_description": sale.material_description or "",
        "material_kanekalon_grams": ""
        if sale.material_kanekalon_grams is None
        else str(sale.material_kanekalon_grams),
        "material_kudri_grams": "" if sale.material_kudri_grams is None else str(sale.material_kudri_grams),
        "material_manual_cost": ""
        if sale.material_manual_cost is None
        else str(sale.material_manual_cost),
        "material_mix_standalone_grams": ""
        if sale.material_mix_standalone_grams is None
        else str(sale.material_mix_standalone_grams),
        "material_mix_source": sale.material_mix_source.value if sale.material_mix_source else "",
        "material_mix_complexity": sale.material_mix_complexity.value
        if sale.material_mix_complexity
        else "",
        "kit_id": str(sale.kit_id or ""),
        "kit_mode": "PIECES",
        "kit_pieces_sold": "" if sale.kit_pieces_sold is None else str(sale.kit_pieces_sold),
        "sale_kit_lines_json": getattr(sale, "kit_lines_json", None) or "",
        "rubber_description": sale.rubber_description or "",
        "rubber_price_override": "" if sale.rubber_price_override is None else str(sale.rubber_price_override),
        "other_description": sale.other_description or "",
    }
    sale_kit_lines_initial = _product_sale_kit_lines_initial(db, fp, sale=sale)
    return templates.TemplateResponse(
        "product_sale_edit.html",
        _ctx(
            request,
            current_user=current_user,
            sale=sale,
            error=err_qp,
            fp=fp,
            selected_client=sale.client,
            default_date=sale.performed_date.date().isoformat(),
            material_services=ms,
            material_services_meta_json=_material_services_meta_json(ms),
            sale_kit_lines_initial=sale_kit_lines_initial,
        ),
    )


@router.post("/{sale_id}/edit")
@legacy_admin_router.post("/{sale_id}/edit")
async def product_sale_edit_save(
    sale_id: int,
    request: Request,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    sale = db.get(ProductSale, sale_id)
    if not sale:
        return RedirectResponse(url="/sales/products?msg=not_found", status_code=303)
    ok, msg = _sale_edit_allowed(db, sale)
    if not ok:
        return RedirectResponse(url=f"/sales/products/{sale_id}?msg=edit_blocked", status_code=303)

    before = SimpleNamespace(**{k: getattr(sale, k) for k in (
        "client_id",
        "performed_date",
        "amount_from_client",
        "kind",
        "material_service_id",
        "material_grams",
        "material_description",
        "material_kanekalon_grams",
        "material_kudri_grams",
        "material_manual_cost",
        "material_mix_source",
        "material_mix_complexity",
        "material_mix_cost_amount",
        "material_mix_bonus_user_id",
        "material_mix_bonus_amount",
        "material_mix_standalone_grams",
        "material_cost_review_pending",
        "kit_id",
        "kit_pieces_sold",
        "kit_breakdown_json",
        "kit_lines_json",
        "rubber_description",
        "rubber_price_override",
        "other_description",
        "is_voided",
        "studio_margin_amount",
    )})

    form = await request.form()
    material_services = _prodazha_materiala_services(db)
    allowed_material_ids = {s.id for s in material_services}

    cid_raw = _g_str(form, "existing_client_id")
    try:
        cid = parse_int(cid_raw, min=1, field_name="existing_client_id")
    except ValueError:
        cid = 0
    if cid <= 0:
        return _render_new(request, current_user, db, error="Выберите клиента из базы.", fp={})
    client = db.get(Client, cid)
    if not client:
        return _render_new(request, current_user, db, error="Клиент не найден.", fp={})

    pd_raw = _g_str(form, "performed_date") or date.today().isoformat()
    try:
        performed = datetime.combine(parse_date_iso(pd_raw, field_name="performed_date"), datetime.min.time())
    except ValueError:
        return _render_new(request, current_user, db, error="Некорректная дата.", fp={})
    try:
        ensure_event_date_in_open_payroll_period(db, performed)
    except ValueError as e:
        return _render_new(request, current_user, db, error=str(e), fp={})

    amount_from_client = _g_int(form, "amount_from_client", 0)
    if amount_from_client < 0:
        return _render_new(request, current_user, db, error="Сумма с клиента не может быть отрицательной.", fp={})

    kind_raw = (_g_str(form, "kind") or "").strip().upper()
    try:
        kind = ProductSaleKind(kind_raw)
    except ValueError:
        return _render_new(request, current_user, db, error="Выберите корректный тип товара.", fp={})

    # Леджер по продаже зависит только от суммы маржи студии и бонуса за смешку.
    prev_ledger_sig = (
        bool(getattr(sale, "is_voided", False)),
        round(float(getattr(sale, "studio_margin_amount", 0.0) or 0.0), 2),
        int(getattr(sale, "material_mix_bonus_user_id", 0) or 0),
        round(float(getattr(sale, "material_mix_bonus_amount", 0.0) or 0.0), 2),
    )

    # revert stock impact from previous state (if any)
    if sale.kind == ProductSaleKind.KIT:
        _revert_kit_sale_stock(db, sale)

    # overwrite common fields
    sale.client_id = client.id
    sale.performed_date = performed
    sale.amount_from_client = int(amount_from_client)
    sale.client_payment_kind = parse_client_payment_kind(_g_str(form, "client_payment_kind"))
    sale.kind = kind

    # reset all kind-specific fields
    sale.material_service_id = None
    sale.material_grams = None
    sale.material_description = None
    sale.material_kanekalon_grams = None
    sale.material_kudri_grams = None
    sale.material_kanekalon_price_per_gram_at_time = None
    sale.material_kudri_price_per_gram_at_time = None
    sale.material_manual_cost = None
    sale.material_mix_source = None
    sale.material_mix_complexity = None
    sale.material_mix_cost_amount = 0.0
    sale.material_mix_bonus_user_id = None
    sale.material_mix_bonus_amount = 0.0
    sale.material_mix_standalone_grams = None
    sale.material_cost_review_pending = False
    sale.kit_id = None
    sale.kit_pieces_sold = None
    sale.kit_breakdown_json = None
    sale.kit_lines_json = None
    sale.rubber_description = None
    sale.rubber_price_override = None
    sale.other_description = None

    if kind == ProductSaleKind.MATERIAL:
        try:
            _apply_material_from_form(db, sale, form, allowed_material_ids, current_user)
        except ValueError as e:
            return RedirectResponse(
                url=f"/sales/products/{sale_id}/edit?err={quote(str(e))}",
                status_code=303,
            )

    elif kind == ProductSaleKind.KIT:
        try:
            parsed_lines = _parse_sale_kit_lines_structured(form)
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        if not parsed_lines:
            raise ValueError("Выберите хотя бы один комплект из наличия.")
        try:
            saved = _apply_parsed_kit_sale_lines(db, client, parsed_lines)
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        sale.kit_id = int(saved[0]["kit_id"])
        sale.kit_pieces_sold = int(sum(int(x["pieces_sold"]) for x in saved))
        sale.kit_breakdown_json = (
            json.dumps(saved[0]["breakdown"], ensure_ascii=False) if saved[0].get("breakdown") else None
        )
        sale.kit_lines_json = json.dumps(saved, ensure_ascii=False)

    elif kind == ProductSaleKind.RUBBER:
        desc = (_g_str(form, "rubber_description") or "").strip()
        if not desc:
            raise ValueError("Для «Хвост/резинка» укажите описание.")
        sale.rubber_description = desc
        override_raw = (_g_str(form, "rubber_price_override") or "").strip()
        if override_raw:
            override = _g_int(form, "rubber_price_override", -1)
            if override < 0:
                raise ValueError("Цена (если указана) должна быть целым числом ≥ 0.")
            sale.rubber_price_override = int(override)

    elif kind == ProductSaleKind.OTHER:
        desc = (_g_str(form, "other_description") or "").strip()
        if not desc:
            raise ValueError("Для «Другое» укажите описание.")
        sale.other_description = desc

    if kind == ProductSaleKind.KIT and sale.kit_id:
        db.refresh(sale, attribute_names=["kit"])
    elif kind == ProductSaleKind.MATERIAL:
        db.refresh(sale, attribute_names=["material_service"])
    try:
        if kind == ProductSaleKind.MATERIAL:
            finalize_material_sale_fields(
                db,
                sale,
                seller_user_id=current_user.id,
                active_role=current_user.role,
            )
        else:
            sale.studio_margin_amount = compute_product_sale_studio_margin(db, sale)
    except ValueError as e:
        db.rollback()
        return RedirectResponse(
            url=f"/sales/products/{sale_id}/edit?err={quote(str(e))}",
            status_code=303,
        )
    sale.updated_at = utcnow_naive()
    sale.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=ProductSaleAuditLog,
        entity_field="sale_id",
        entity_id=sale.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(
            before,
            sale,
            (
                "client_id",
                "performed_date",
                "amount_from_client",
                "kind",
                "material_service_id",
                "material_grams",
                "material_description",
                "material_kanekalon_grams",
                "material_kudri_grams",
                "material_manual_cost",
                "material_mix_source",
                "material_mix_complexity",
                "material_mix_cost_amount",
                "material_mix_bonus_user_id",
                "material_mix_bonus_amount",
                "material_mix_standalone_grams",
                "material_cost_review_pending",
                "kit_id",
                "kit_pieces_sold",
                "kit_breakdown_json",
                "kit_lines_json",
                "rubber_description",
                "rubber_price_override",
                "other_description",
                "studio_margin_amount",
            ),
        ),
    )
    new_ledger_sig = (
        bool(getattr(sale, "is_voided", False)),
        round(float(getattr(sale, "studio_margin_amount", 0.0) or 0.0), 2),
        int(getattr(sale, "material_mix_bonus_user_id", 0) or 0),
        round(float(getattr(sale, "material_mix_bonus_amount", 0.0) or 0.0), 2),
    )
    if new_ledger_sig != prev_ledger_sig:
        replace_product_sale_studio_accrual(db, sale, current_user.id)
    db.commit()
    return RedirectResponse(url=f"/sales/products/{sale_id}?msg=saved", status_code=303)


@router.post("/{sale_id}/void")
@legacy_admin_router.post("/{sale_id}/void")
async def product_sale_void(
    sale_id: int,
    request: Request,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    sale = db.get(ProductSale, sale_id)
    if not sale:
        return RedirectResponse(url="/sales/products?msg=not_found", status_code=303)
    ok, _ = _sale_edit_allowed(db, sale)
    if not ok:
        return RedirectResponse(url=f"/sales/products/{sale_id}?msg=void_blocked", status_code=303)

    storno_source_accruals(db, PayrollFundSourceKind.PRODUCT_SALE, sale.id, current_user.id)

    # revert stock impact
    if sale.kind == ProductSaleKind.KIT:
        _revert_kit_sale_stock(db, sale)

    before = SimpleNamespace(is_voided=sale.is_voided, voided_at=sale.voided_at, voided_by_user_id=sale.voided_by_user_id)
    sale.is_voided = True
    sale.voided_at = utcnow_naive()
    sale.voided_by_user_id = current_user.id
    sale.updated_at = utcnow_naive()
    sale.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=ProductSaleAuditLog,
        entity_field="sale_id",
        entity_id=sale.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before, sale, ("is_voided", "voided_at", "voided_by_user_id")),
    )
    db.commit()
    return RedirectResponse(url=f"/sales/products/{sale_id}?msg=voided", status_code=303)

@router.post("/new")
@legacy_admin_router.post("/new")
async def product_sale_new_post(
    request: Request,
    current_user: AuthUser = _STAFF,
    db: Session = Depends(get_db),
):
    form = await request.form()
    material_services = _prodazha_materiala_services(db)
    allowed_material_ids = {s.id for s in material_services}

    fp = {
        "existing_client_id": _g_str(form, "existing_client_id"),
        "performed_date": _g_str(form, "performed_date") or date.today().isoformat(),
        "amount_from_client": _g_str(form, "amount_from_client"),
        "client_payment_kind": _g_str(form, "client_payment_kind"),
        "kind": _g_str(form, "kind") or ProductSaleKind.MATERIAL.value,
        # material
        "material_service_id": _g_str(form, "material_service_id"),
        "material_grams": _g_str(form, "material_grams"),
        "material_description": _g_str(form, "material_description"),
        "material_kanekalon_grams": _g_str(form, "material_kanekalon_grams"),
        "material_kudri_grams": _g_str(form, "material_kudri_grams"),
        "material_manual_cost": _g_str(form, "material_manual_cost"),
        "material_mix_standalone_grams": _g_str(form, "material_mix_standalone_grams"),
        "material_mix_source": _g_str(form, "material_mix_source"),
        "material_mix_complexity": _g_str(form, "material_mix_complexity"),
        # kit
        "kit_id": _g_str(form, "kit_id"),
        "kit_mode": _g_str(form, "kit_mode") or "PIECES",
        "kit_pieces_sold": _g_str(form, "kit_pieces_sold"),
        "sale_kit_lines_json": _g_str(form, "sale_kit_lines_json"),
        # rubber
        "rubber_description": _g_str(form, "rubber_description"),
        "rubber_price_override": _g_str(form, "rubber_price_override"),
        # other
        "other_description": _g_str(form, "other_description"),
    }

    def _fail(msg: str):
        return _render_new(request, current_user, db, error=msg, fp=fp)

    cid_raw = fp["existing_client_id"]
    try:
        cid = parse_int(cid_raw, min=1, field_name="existing_client_id")
    except ValueError:
        cid = 0
    if cid <= 0:
        return _fail("Выберите клиента из базы.")
    client = db.get(Client, cid)
    if not client:
        return _fail("Клиент не найден.")

    pd_raw = fp["performed_date"]
    try:
        performed = datetime.combine(parse_date_iso(pd_raw, field_name="performed_date"), datetime.min.time())
    except ValueError:
        return _fail("Некорректная дата.")
    try:
        ensure_event_date_in_open_payroll_period(db, performed)
    except ValueError as e:
        return _fail(str(e))

    amount_from_client = _g_int(form, "amount_from_client", 0)
    if amount_from_client < 0:
        return _fail("Сумма с клиента не может быть отрицательной.")
    client_payment_kind = parse_client_payment_kind(_g_str(form, "client_payment_kind"))

    kind_raw = (fp["kind"] or "").strip().upper()
    try:
        kind = ProductSaleKind(kind_raw)
    except ValueError:
        return _fail("Выберите корректный тип товара.")

    row = ProductSale(
        created_by_user_id=current_user.id,
        performed_date=performed,
        client_id=client.id,
        amount_from_client=amount_from_client,
        client_payment_kind=client_payment_kind,
        kind=kind,
    )
    bid_raw = (_g_str(form, "booking_id") or "").strip()
    try:
        bid = parse_int(bid_raw, min=1, field_name="booking_id") if bid_raw else 0
    except ValueError:
        bid = 0
    if bid > 0:
        row.booking_id = bid

    if kind == ProductSaleKind.MATERIAL:
        try:
            _apply_material_from_form(db, row, form, allowed_material_ids, current_user)
        except ValueError as e:
            return _fail(str(e))

    elif kind == ProductSaleKind.KIT:
        try:
            parsed_lines = _parse_sale_kit_lines_structured(form)
        except ValueError as e:
            return _fail(str(e))
        if not parsed_lines:
            return _fail("Выберите хотя бы один комплект из наличия.")
        try:
            saved = _apply_parsed_kit_sale_lines(db, client, parsed_lines)
        except ValueError as e:
            return _fail(str(e))
        row.kit_id = int(saved[0]["kit_id"])
        row.kit_pieces_sold = int(sum(int(x["pieces_sold"]) for x in saved))
        row.kit_breakdown_json = (
            json.dumps(saved[0]["breakdown"], ensure_ascii=False) if saved[0].get("breakdown") else None
        )
        row.kit_lines_json = json.dumps(saved, ensure_ascii=False)

    elif kind == ProductSaleKind.RUBBER:
        desc = (fp["rubber_description"] or "").strip()
        if not desc:
            return _fail("Для «Хвост/резинка» укажите описание.")
        row.rubber_description = desc

        override_raw = (fp["rubber_price_override"] or "").strip()
        if override_raw:
            override = _g_int(form, "rubber_price_override", -1)
            if override < 0:
                return _fail("Цена (если указана) должна быть целым числом ≥ 0.")
            row.rubber_price_override = override

    elif kind == ProductSaleKind.OTHER:
        desc = (fp["other_description"] or "").strip()
        if not desc:
            return _fail("Для «Другое» укажите описание.")
        row.other_description = desc

    db.add(row)
    db.flush()
    if kind == ProductSaleKind.KIT and row.kit_id:
        db.refresh(row, attribute_names=["kit"])
    elif kind == ProductSaleKind.MATERIAL:
        db.refresh(row, attribute_names=["material_service"])
    try:
        if kind == ProductSaleKind.MATERIAL:
            finalize_material_sale_fields(
                db,
                row,
                seller_user_id=current_user.id,
                active_role=current_user.role,
            )
        else:
            row.studio_margin_amount = compute_product_sale_studio_margin(db, row)
    except ValueError as e:
        db.rollback()
        return _fail(str(e))
    post_product_sale_studio_accrual(db, row, current_user.id)
    bid_for_auto_complete = row.booking_id
    db.commit()
    if bid_for_auto_complete:
        from app.routes.bookings import try_auto_complete_booking

        try_auto_complete_booking(db, int(bid_for_auto_complete))
        db.commit()
    return RedirectResponse(url=f"/sales/products/{row.id}?msg=created", status_code=303)

