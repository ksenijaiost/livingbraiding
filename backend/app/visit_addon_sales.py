"""Доп. продажи визита: несколько строк в JSON, процент продавцу, остаток в фонд студии."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import User, UserRole
from app.forms_parse import parse_bool, parse_float
from app.sale_percent_options import DEFAULT_SALE_PERCENTS, list_sale_percents
from app.user_roles import select_users_with_any_role

ADDON_SALE_SELLER_COMMENT = "Процент с доп. продажи"
ADDON_SALE_STUDIO_COMMENT = "Доп. продажа в фонд студии"
_SELLER_ROLES = (UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)


def money_q2(x: float) -> float:
    return round(float(x), 2)


@dataclass
class AddonSaleLine:
    price: float
    description: str = ""
    included_in_cost: bool = False
    service_no: int | None = None
    sale_percent: int = 0
    seller_user_id: int = 0

    @property
    def commission(self) -> float:
        return money_q2(float(self.price or 0) * (int(self.sale_percent or 0) / 100.0))

    @property
    def studio_amount(self) -> float:
        return money_q2(max(0.0, float(self.price or 0) - self.commission))


@dataclass
class AddonSalesInput:
    mode: str = "all"
    lines: list[AddonSaleLine] = field(default_factory=list)
    shared_included_in_cost: bool = False
    shared_service_no: int | None = None
    shared_sale_percent: int | None = None
    shared_seller_user_id: int | None = None

    def to_json(self) -> str | None:
        if not self.lines:
            return None
        payload = {
            "v": 1,
            "mode": "each" if self.mode == "each" else "all",
            "shared": {
                "included_in_cost": bool(self.shared_included_in_cost),
                "service_no": self.shared_service_no,
                "sale_percent": self.shared_sale_percent,
                "seller_user_id": self.shared_seller_user_id,
            },
            "lines": [
                {
                    "price": float(line.price),
                    "description": line.description,
                    "included_in_cost": bool(line.included_in_cost),
                    "service_no": line.service_no,
                    "sale_percent": int(line.sale_percent),
                    "seller_user_id": int(line.seller_user_id),
                }
                for line in self.lines
            ],
        }
        return json.dumps(payload, ensure_ascii=False)


def addon_sales_from_visit_json(raw: str | None) -> AddonSalesInput | None:
    data = _json_obj(raw)
    if int(data.get("v") or 0) != 1 or not isinstance(data.get("lines"), list):
        return None
    lines: list[AddonSaleLine] = []
    for item in data["lines"]:
        if not isinstance(item, dict):
            continue
        try:
            price = max(0.0, float(item.get("price") or 0))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        service_no = _optional_int(item.get("service_no"))
        lines.append(
            AddonSaleLine(
                price=price,
                description=str(item.get("description") or "").strip(),
                included_in_cost=bool(item.get("included_in_cost")),
                service_no=service_no,
                sale_percent=int(item.get("sale_percent") or 0),
                seller_user_id=int(item.get("seller_user_id") or 0),
            )
        )
    shared = data.get("shared") if isinstance(data.get("shared"), dict) else {}
    return AddonSalesInput(
        mode="each" if str(data.get("mode") or "") == "each" else "all",
        lines=lines,
        shared_included_in_cost=bool(shared.get("included_in_cost")),
        shared_service_no=_optional_int(shared.get("service_no")),
        shared_sale_percent=_optional_int(shared.get("sale_percent")),
        shared_seller_user_id=_optional_int(shared.get("seller_user_id")),
    )


def addon_revenue_total(raw: str | None) -> float:
    sales = addon_sales_from_visit_json(raw)
    if sales is None:
        return 0.0
    return money_q2(sum(line.price for line in sales.lines if not line.included_in_cost))


def addon_seller_commission_by_user(raw: str | None) -> dict[int, float]:
    sales = addon_sales_from_visit_json(raw)
    if sales is None:
        return {}
    out: dict[int, float] = {}
    for line in sales.lines:
        if line.seller_user_id <= 0 or line.commission <= 0:
            continue
        out[line.seller_user_id] = money_q2(out.get(line.seller_user_id, 0.0) + line.commission)
    return out


def addon_studio_amount(raw: str | None) -> float:
    sales = addon_sales_from_visit_json(raw)
    if sales is None:
        return 0.0
    return money_q2(sum(line.studio_amount for line in sales.lines))


def addon_card_rows(db: Session, raw: str | None) -> list[dict[str, Any]]:
    sales = addon_sales_from_visit_json(raw)
    if sales is None:
        return []
    rows: list[dict[str, Any]] = []
    for line in sales.lines:
        user = db.get(User, int(line.seller_user_id)) if line.seller_user_id else None
        if user is not None:
            seller_name = (user.display_name or user.username or "").strip() or f"#{user.id}"
        else:
            seller_name = "—"
        rows.append(
            {
                "price": line.price,
                "description": line.description,
                "seller_name": seller_name,
                "sale_percent": int(line.sale_percent),
                "commission": line.commission,
                "included_in_cost": line.included_in_cost,
                "service_no": line.service_no,
            }
        )
    return rows


def addon_price_total(raw: str | None) -> float:
    sales = addon_sales_from_visit_json(raw)
    if sales is None:
        return 0.0
    return money_q2(sum(line.price for line in sales.lines))


def seller_users(db: Session) -> list[User]:
    return list(
        db.scalars(
            select_users_with_any_role(*_SELLER_ROLES).order_by(User.display_name.asc(), User.username.asc())
        ).all()
    )


def parse_addon_sales_from_form(
    form: Any,
    *,
    service_count: int,
    db: Session | None = None,
) -> AddonSalesInput:
    allowed = list(list_sale_percents(db)) if db is not None else list(DEFAULT_SALE_PERCENTS)
    sellers = {int(u.id) for u in seller_users(db)} if db is not None else None
    mode = "each" if _g(form, "addon_sales_mode") == "each" else "all"
    shared_included = parse_bool(_g(form, "addon_shared_included"))
    shared_service = _service_no(_g(form, "addon_shared_service_no"), service_count, required=False)
    shared_percent = _percent_or_none(_g(form, "addon_shared_percent"), allowed)
    shared_seller = _seller_or_none(_g(form, "addon_shared_seller"), sellers)

    lines: list[AddonSaleLine] = []
    for idx in _row_indices(form):
        price_raw = _g(form, f"addon_{idx}_price")
        description = _g(form, f"addon_{idx}_description")
        if not price_raw and not description:
            continue
        if not price_raw:
            raise ValueError("Укажите цену доп. продажи.")
        price = parse_float(price_raw, min=0.0, field_name=f"addon_{idx}_price")
        if price <= 0:
            raise ValueError("Цена доп. продажи должна быть больше нуля.")
        if mode == "each":
            included = parse_bool(_g(form, f"addon_{idx}_included"))
            percent = _percent_required(_g(form, f"addon_{idx}_percent"), allowed)
            seller = _seller_required(_g(form, f"addon_{idx}_seller"), sellers)
            service_no = _resolve_service_no(
                _g(form, f"addon_{idx}_service_no"),
                service_count=service_count,
                included=included,
            )
        else:
            included = shared_included
            if shared_percent is None:
                raise ValueError("Укажите процент с продажи.")
            if shared_seller is None:
                raise ValueError("Укажите сотрудника-продавца.")
            percent = shared_percent
            seller = shared_seller
            service_no = _resolve_service_no(
                "" if shared_service is None else str(shared_service),
                service_count=service_count,
                included=included,
            )
        lines.append(
            AddonSaleLine(
                price=price,
                description=description,
                included_in_cost=included,
                service_no=service_no,
                sale_percent=percent,
                seller_user_id=seller,
            )
        )
    return AddonSalesInput(
        mode=mode,
        lines=lines,
        shared_included_in_cost=shared_included,
        shared_service_no=shared_service,
        shared_sale_percent=shared_percent,
        shared_seller_user_id=shared_seller,
    )


def apply_addon_sales_to_lines(lines: list[Any], sales: AddonSalesInput) -> None:
    """Цена с галочкой садится себестоимостью выбранной услуги. Без галочки услугу не увеличивает."""
    for line in lines:
        line.addon_sales_amount = 0.0
        line.addon_sales_description = ""
        line.addon_client_amount = 0.0
    descriptions: dict[int, list[str]] = {}
    for sale in sales.lines:
        if not sale.included_in_cost or not sale.service_no:
            continue
        idx = int(sale.service_no) - 1
        if idx < 0 or idx >= len(lines):
            continue
        lines[idx].addon_sales_amount = float(lines[idx].addon_sales_amount or 0) + float(sale.price)
        if sale.description:
            descriptions.setdefault(idx, []).append(sale.description)
    for idx, parts in descriptions.items():
        lines[idx].addon_sales_description = "; ".join(parts)


def persist_visit_addon_sales(visit: Any, sales: AddonSalesInput) -> None:
    visit.addons_details_json = sales.to_json()
    extra = money_q2(sum(line.price for line in sales.lines if not line.included_in_cost))
    visit.amount_from_client = money_q2(float(visit.amount_from_client or 0) + extra)


def ensure_addon_form_prefill(fp: dict[str, str]) -> None:
    """Собрать JSON для формы из уже разобранных полей, если его ещё нет."""
    if (fp.get("addon_sales_json") or "").strip():
        return
    indices = sorted(
        int(match.group(1))
        for key in fp
        if (match := re.fullmatch(r"addon_(\d+)_price", str(key)))
    )
    if not indices:
        return
    mode = "each" if fp.get("addon_sales_mode") == "each" else "all"
    lines = []
    for idx in indices:
        price = (fp.get(f"addon_{idx}_price") or "").strip()
        description = (fp.get(f"addon_{idx}_description") or "").strip()
        if not price and not description:
            continue
        if mode == "each":
            included = parse_bool(fp.get(f"addon_{idx}_included") or "")
            service_no = _optional_int(fp.get(f"addon_{idx}_service_no"))
            percent = _optional_int(fp.get(f"addon_{idx}_percent"))
            seller = _optional_int(fp.get(f"addon_{idx}_seller"))
        else:
            included = parse_bool(fp.get("addon_shared_included") or "")
            service_no = _optional_int(fp.get("addon_shared_service_no"))
            percent = _optional_int(fp.get("addon_shared_percent"))
            seller = _optional_int(fp.get("addon_shared_seller"))
        lines.append(
            {
                "price": price,
                "description": description,
                "included_in_cost": included,
                "service_no": service_no,
                "sale_percent": percent,
                "seller_user_id": seller,
            }
        )
    if not lines:
        return
    fp["addon_sales_json"] = json.dumps(
        {
            "v": 1,
            "mode": mode,
            "shared": {
                "included_in_cost": parse_bool(fp.get("addon_shared_included") or ""),
                "service_no": _optional_int(fp.get("addon_shared_service_no")),
                "sale_percent": _optional_int(fp.get("addon_shared_percent")),
                "seller_user_id": _optional_int(fp.get("addon_shared_seller")),
            },
            "lines": lines,
        },
        ensure_ascii=False,
    )


def legacy_addon_prefill_json(
    *,
    amount: float,
    description: str,
    included_in_cost: bool,
    service_no: int,
) -> str:
    payload = {
        "v": 1,
        "mode": "all",
        "shared": {
            "included_in_cost": included_in_cost,
            "service_no": service_no,
            "sale_percent": None,
            "seller_user_id": None,
        },
        "lines": [
            {
                "price": amount,
                "description": description,
                "included_in_cost": included_in_cost,
                "service_no": service_no,
                "sale_percent": None,
                "seller_user_id": None,
            }
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def _resolve_service_no(raw: str, *, service_count: int, included: bool) -> int | None:
    if service_count <= 1:
        return 1 if service_count == 1 else None
    no = _service_no(raw, service_count, required=False)
    if included and no is None:
        raise ValueError("Для доп. продажи, включённой в стоимость, выберите услугу.")
    return no


def _service_no(raw: str, service_count: int, *, required: bool) -> int | None:
    s = (raw or "").strip()
    if not s:
        if required:
            raise ValueError("Выберите услугу для доп. продажи.")
        return None
    try:
        no = int(parse_float(s, field_name="addon_service_no"))
    except ValueError as exc:
        raise ValueError("Выберите услугу для доп. продажи.") from exc
    if no < 1 or no > max(service_count, 1):
        raise ValueError("Выберите услугу для доп. продажи.")
    return no


def _percent_required(raw: str, allowed: list[int]) -> int:
    pct = _percent_or_none(raw, allowed)
    if pct is None:
        labels = ", ".join(f"{p}%" for p in allowed) or "нет доступных"
        raise ValueError(f"Выберите процент с продажи: {labels}.")
    return pct


def _percent_or_none(raw: str, allowed: list[int]) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        pct = int(parse_float(s, field_name="addon_sale_percent"))
    except ValueError as exc:
        labels = ", ".join(f"{p}%" for p in allowed)
        raise ValueError(f"Выберите процент с продажи: {labels}.") from exc
    if pct not in allowed:
        labels = ", ".join(f"{p}%" for p in allowed)
        raise ValueError(f"Выберите процент с продажи: {labels}.")
    return pct


def _seller_required(raw: str, sellers: set[int] | None) -> int:
    seller = _seller_or_none(raw, sellers)
    if seller is None:
        raise ValueError("Укажите сотрудника-продавца.")
    return seller


def _seller_or_none(raw: str, sellers: set[int] | None) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        uid = int(parse_float(s, field_name="addon_seller"))
    except ValueError as exc:
        raise ValueError("Укажите сотрудника-продавца.") from exc
    if uid <= 0:
        raise ValueError("Укажите сотрудника-продавца.")
    if sellers is not None and uid not in sellers:
        raise ValueError("Сотрудник не может оформлять продажи.")
    return uid


def _row_indices(form: Any) -> list[int]:
    found: list[int] = []
    keys = form.keys() if hasattr(form, "keys") else []
    for key in keys:
        match = re.fullmatch(r"addon_(\d+)_price", str(key))
        if match:
            found.append(int(match.group(1)))
    return sorted(set(found))


def _g(form: Any, name: str) -> str:
    raw = form.get(name) if hasattr(form, "get") else None
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode().strip()
    return str(raw).strip()


def _json_obj(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _optional_int(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
