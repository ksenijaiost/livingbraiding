"""
Этап 6.4: продажа товаров без услуги (материал/комплект/резинки/другое).
Доступ: мастер и админы.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import UploadFile

from app.auth import AuthUser, require_role
from app.db.models import (
    Client,
    Kit,
    ProductSale,
    ProductSaleKind,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    UserRole,
)
from app.db.session import get_db
from app.visit_edit_policy import edit_window_days, is_in_closed_payroll_period, within_edit_window

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(prefix="/sales/products", tags=["product-sales"])
_STAFF = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER))
_SUPER = Depends(require_role(UserRole.ADMIN_SUPER))


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


def _apply_kit_delta(db: Session, kit_id: int, delta: int) -> None:
    kit = db.get(Kit, kit_id)
    if not kit:
        raise ValueError("Комплект не найден.")
    new_avail = int(kit.pieces_available + delta)
    if new_avail < 0:
        raise ValueError("Недостаточно заготовок в наличии для этой операции.")
    kit.pieces_available = new_avail


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
        return int(float(s.replace(",", ".")))
    except ValueError:
        return default


def _g_float(form: Any, name: str, default: float = 0.0) -> float:
    s = _g_str(form, name, "")
    if not s:
        return default
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return default


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
    if eid.isdigit():
        selected_client = db.get(Client, int(eid))
    default_date = (fp.get("performed_date") or "").strip() or date.today().isoformat()
    return templates.TemplateResponse(
        "product_sale_new.html",
        _ctx(
            request,
            current_user=current_user,
            error=error,
            fp=fp,
            selected_client=selected_client,
            default_date=default_date,
            material_services=material_services,
        ),
        status_code=400 if error else 200,
    )


@router.get("", response_class=HTMLResponse)
def product_sales_list(
    request: Request,
    current_user: AuthUser = _STAFF,
    db: Session = Depends(get_db),
):
    msg = request.query_params.get("msg")
    rows = list(
        db.scalars(
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
        ).all()
    )
    return templates.TemplateResponse(
        "product_sales_list.html",
        _ctx(request, current_user=current_user, rows=rows, msg=msg),
    )


@router.get("/new", response_class=HTMLResponse)
def product_sale_new_get(
    request: Request,
    current_user: AuthUser = _STAFF,
    db: Session = Depends(get_db),
):
    return _render_new(request, current_user, db)


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
            ),
            status_code=404,
        )
    edit_allowed, edit_block_msg = _sale_edit_allowed(db, sale)
    can_edit = current_user.role == UserRole.ADMIN_SUPER
    return templates.TemplateResponse(
        "product_sale_detail.html",
        _ctx(
            request,
            current_user=current_user,
            sale=sale,
            error=None,
            can_edit=can_edit,
            edit_allowed=edit_allowed,
            edit_block_msg=edit_block_msg,
            ru_kind=_ru_kind(sale.kind),
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
            ),
            status_code=404,
        )
    ok, msg = _sale_edit_allowed(db, sale)
    if not ok:
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
                material_services=_prodazha_materiala_services(db),
            ),
            status_code=403,
        )
    fp = {
        "existing_client_id": str(sale.client_id),
        "performed_date": sale.performed_date.date().isoformat(),
        "amount_from_client": str(sale.amount_from_client),
        "kind": sale.kind.value,
        "material_service_id": str(sale.material_service_id or ""),
        "material_grams": "" if sale.material_grams is None else str(sale.material_grams),
        "material_description": sale.material_description or "",
        "kit_id": str(sale.kit_id or ""),
        "kit_mode": "PIECES",
        "kit_pieces_sold": "" if sale.kit_pieces_sold is None else str(sale.kit_pieces_sold),
        "rubber_description": sale.rubber_description or "",
        "rubber_price_override": "" if sale.rubber_price_override is None else str(sale.rubber_price_override),
        "other_description": sale.other_description or "",
    }
    return templates.TemplateResponse(
        "product_sale_edit.html",
        _ctx(
            request,
            current_user=current_user,
            sale=sale,
            error=None,
            fp=fp,
            selected_client=sale.client,
            default_date=sale.performed_date.date().isoformat(),
            material_services=_prodazha_materiala_services(db),
        ),
    )


@router.post("/{sale_id}/edit")
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

    form = await request.form()
    material_services = _prodazha_materiala_services(db)
    allowed_material_ids = {s.id for s in material_services}

    cid_raw = _g_str(form, "existing_client_id")
    if not cid_raw.isdigit():
        return _render_new(request, current_user, db, error="Выберите клиента из базы.", fp={})
    client = db.get(Client, int(cid_raw))
    if not client:
        return _render_new(request, current_user, db, error="Клиент не найден.", fp={})

    pd_raw = _g_str(form, "performed_date") or date.today().isoformat()
    try:
        performed = datetime.combine(date.fromisoformat(pd_raw), datetime.min.time())
    except ValueError:
        return _render_new(request, current_user, db, error="Некорректная дата.", fp={})

    amount_from_client = _g_int(form, "amount_from_client", 0)
    if amount_from_client < 0:
        return _render_new(request, current_user, db, error="Сумма с клиента не может быть отрицательной.", fp={})

    kind_raw = (_g_str(form, "kind") or "").strip().upper()
    try:
        kind = ProductSaleKind(kind_raw)
    except ValueError:
        return _render_new(request, current_user, db, error="Выберите корректный тип товара.", fp={})

    # revert stock impact from previous state (if any)
    if sale.kind == ProductSaleKind.KIT and sale.kit_id and sale.kit_pieces_sold:
        _apply_kit_delta(db, sale.kit_id, int(sale.kit_pieces_sold))

    # overwrite common fields
    sale.client_id = client.id
    sale.performed_date = performed
    sale.amount_from_client = int(amount_from_client)
    sale.kind = kind

    # reset all kind-specific fields
    sale.material_service_id = None
    sale.material_grams = None
    sale.material_description = None
    sale.kit_id = None
    sale.kit_pieces_sold = None
    sale.rubber_description = None
    sale.rubber_price_override = None
    sale.other_description = None

    if kind == ProductSaleKind.MATERIAL:
        sid_raw = _g_str(form, "material_service_id")
        service_id = int(sid_raw) if sid_raw.isdigit() else None
        if service_id and service_id not in allowed_material_ids:
            raise ValueError("Недопустимая услуга «Продажа материала».")
        grams = _g_float(form, "material_grams", 0.0)
        if grams <= 0:
            raise ValueError("Для «Материал» укажите количество грамм больше 0.")
        sale.material_service_id = service_id
        sale.material_grams = float(grams)
        sale.material_description = (_g_str(form, "material_description") or "").strip() or None

    elif kind == ProductSaleKind.KIT:
        kid_raw = _g_str(form, "kit_id")
        if not kid_raw.isdigit():
            raise ValueError("Выберите комплект из наличия.")
        kit = db.get(Kit, int(kid_raw))
        if not kit:
            raise ValueError("Комплект не найден.")
        if kit.stock_price_total is None or float(kit.stock_price_total) <= 0:
            raise ValueError(
                "У этого комплекта не задана цена продажи — списание невозможно. Укажите цену в карточке комплекта (администратор)."
            )
        mode = (_g_str(form, "kit_mode") or "PIECES").strip().upper()
        if mode not in ("PIECES", "ALL"):
            raise ValueError("Некорректный режим продажи комплекта.")
        pieces_to_sell = kit.pieces_available
        if mode == "PIECES":
            pieces_to_sell = _g_int(form, "kit_pieces_sold", 0)
            if pieces_to_sell <= 0:
                raise ValueError("Укажите количество заготовок больше 0.")
            if pieces_to_sell > kit.pieces_available:
                raise ValueError("Нельзя продать больше заготовок, чем есть в наличии.")
        _apply_kit_delta(db, kit.id, -int(pieces_to_sell))
        sale.kit_id = kit.id
        sale.kit_pieces_sold = int(pieces_to_sell)

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

    db.commit()
    return RedirectResponse(url=f"/sales/products/{sale_id}?msg=saved", status_code=303)


@router.post("/{sale_id}/void")
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

    # revert stock impact
    if sale.kind == ProductSaleKind.KIT and sale.kit_id and sale.kit_pieces_sold:
        _apply_kit_delta(db, sale.kit_id, int(sale.kit_pieces_sold))

    sale.is_voided = True
    sale.voided_at = datetime.utcnow()
    sale.voided_by_user_id = current_user.id
    db.commit()
    return RedirectResponse(url=f"/sales/products/{sale_id}?msg=voided", status_code=303)

@router.post("/new")
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
        "kind": _g_str(form, "kind") or ProductSaleKind.MATERIAL.value,
        # material
        "material_service_id": _g_str(form, "material_service_id"),
        "material_grams": _g_str(form, "material_grams"),
        "material_description": _g_str(form, "material_description"),
        # kit
        "kit_id": _g_str(form, "kit_id"),
        "kit_mode": _g_str(form, "kit_mode") or "PIECES",
        "kit_pieces_sold": _g_str(form, "kit_pieces_sold"),
        # rubber
        "rubber_description": _g_str(form, "rubber_description"),
        "rubber_price_override": _g_str(form, "rubber_price_override"),
        # other
        "other_description": _g_str(form, "other_description"),
    }

    def _fail(msg: str):
        return _render_new(request, current_user, db, error=msg, fp=fp)

    cid_raw = fp["existing_client_id"]
    if not cid_raw.isdigit():
        return _fail("Выберите клиента из базы.")
    client = db.get(Client, int(cid_raw))
    if not client:
        return _fail("Клиент не найден.")

    pd_raw = fp["performed_date"]
    try:
        performed = datetime.combine(date.fromisoformat(pd_raw), datetime.min.time())
    except ValueError:
        return _fail("Некорректная дата.")

    amount_from_client = _g_int(form, "amount_from_client", 0)
    if amount_from_client < 0:
        return _fail("Сумма с клиента не может быть отрицательной.")

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
        kind=kind,
    )

    if kind == ProductSaleKind.MATERIAL:
        sid_raw = (fp["material_service_id"] or "").strip()
        service_id = int(sid_raw) if sid_raw.isdigit() else None
        if service_id and service_id not in allowed_material_ids:
            return _fail("Выберите услугу из списка «Продажа материала» или оставьте «не указано».")

        grams = _g_float(form, "material_grams", 0.0)
        if grams <= 0:
            return _fail("Для «Материал» укажите количество грамм больше 0.")

        row.material_service_id = service_id
        row.material_grams = grams
        row.material_description = (fp["material_description"] or "").strip() or None

    elif kind == ProductSaleKind.KIT:
        kid_raw = (fp["kit_id"] or "").strip()
        if not kid_raw.isdigit():
            return _fail("Выберите комплект из наличия.")
        kit = db.get(Kit, int(kid_raw))
        if not kit:
            return _fail("Комплект не найден.")
        if kit.stock_price_total is None or float(kit.stock_price_total) <= 0:
            return _fail(
                "У этого комплекта не задана цена продажи — укажите цену в карточке комплекта (администратор)."
            )

        mode = (fp["kit_mode"] or "PIECES").strip().upper()
        if mode not in ("PIECES", "ALL"):
            return _fail("Некорректный режим продажи комплекта.")

        pieces_to_sell = kit.pieces_available
        if mode == "PIECES":
            pieces_to_sell = _g_int(form, "kit_pieces_sold", 0)
            if pieces_to_sell <= 0:
                return _fail("Укажите количество заготовок больше 0.")
            if pieces_to_sell > kit.pieces_available:
                return _fail("Нельзя продать больше заготовок, чем есть в наличии.")

        kit.pieces_available = int(kit.pieces_available - pieces_to_sell)
        row.kit_id = kit.id
        row.kit_pieces_sold = int(pieces_to_sell)

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
    db.commit()
    return RedirectResponse(url="/sales/products?msg=saved", status_code=303)

