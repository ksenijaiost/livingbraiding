"""
Этап «Товары» (1): продажа материала из наличия — отдельно от визита.
Доступ: мастер и админы. Категория каталога «Продажа материала».
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
from app.db.models import Client, MaterialRetailSale, Service, ServiceCategory, ServiceSubcategory, UserRole
from app.db.session import get_db
templates = Jinja2Templates(directory="app/templates")

router = APIRouter(prefix="/sales/material", tags=["retail-material"])
_STAFF = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER))


def _ctx(request: Request, current_user: AuthUser, **kwargs):
    return {"request": request, "current_user": current_user, **kwargs}


def _prodazha_services(db: Session) -> list[Service]:
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


def _render_new(
    request: Request,
    current_user: AuthUser,
    db: Session,
    *,
    error: str | None = None,
    fp: dict | None = None,
):
    fp = fp or {}
    svcs = _prodazha_services(db)
    selected_client = None
    eid = (fp.get("existing_client_id") or "").strip()
    if eid.isdigit():
        selected_client = db.get(Client, int(eid))
    default_date = (fp.get("performed_date") or "").strip() or date.today().isoformat()
    return templates.TemplateResponse(
        "retail_material_sale_new.html",
        _ctx(
            request,
            current_user=current_user,
            services=svcs,
            error=error,
            fp=fp,
            selected_client=selected_client,
            default_date=default_date,
        ),
        status_code=400 if error else 200,
    )


@router.get("/new", response_class=HTMLResponse)
def retail_material_sale_new_get(
    request: Request,
    current_user: AuthUser = _STAFF,
    db: Session = Depends(get_db),
):
    return _render_new(request, current_user, db)


@router.get("", response_class=HTMLResponse)
def retail_material_sale_list(
    request: Request,
    current_user: AuthUser = _STAFF,
    db: Session = Depends(get_db),
):
    msg = request.query_params.get("msg")
    rows = list(
        db.scalars(
            select(MaterialRetailSale)
            .options(
                selectinload(MaterialRetailSale.client),
                selectinload(MaterialRetailSale.created_by_user),
                selectinload(MaterialRetailSale.service),
            )
            .order_by(MaterialRetailSale.created_at.desc())
            .limit(150)
        ).all()
    )
    return templates.TemplateResponse(
        "retail_material_sales_list.html",
        _ctx(request, current_user=current_user, rows=rows, msg=msg),
    )


@router.post("/new")
async def retail_material_sale_new_post(
    request: Request,
    current_user: AuthUser = _STAFF,
    db: Session = Depends(get_db),
):
    form = await request.form()
    svcs = _prodazha_services(db)
    fp = {
        "existing_client_id": _g_str(form, "existing_client_id"),
        "performed_date": _g_str(form, "performed_date") or date.today().isoformat(),
        "service_id": _g_str(form, "service_id"),
        "grams": _g_str(form, "grams"),
        "sale_price": _g_str(form, "sale_price"),
        "description": _g_str(form, "description"),
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

    try:
        grams = float((fp["grams"] or "0").replace(",", "."))
    except ValueError:
        return _fail("Укажите количество грамм.")

    if grams <= 0:
        return _fail("Количество грамм должно быть больше 0.")

    try:
        sale_price = int(float((fp["sale_price"] or "0").replace(",", ".")))
    except ValueError:
        return _fail("Укажите стоимость продажи (целое число).")

    if sale_price < 0:
        return _fail("Стоимость не может быть отрицательной.")

    desc = fp["description"] or None
    sid_raw = fp["service_id"]
    service_id = int(sid_raw) if sid_raw.isdigit() else None
    if service_id:
        allowed_ids = {s.id for s in svcs}
        if service_id not in allowed_ids:
            return _fail("Выберите услугу из списка «Продажа материала» или оставьте «не указано».")

    row = MaterialRetailSale(
        created_by_user_id=current_user.id,
        performed_date=performed,
        client_id=client.id,
        service_id=service_id,
        grams=grams,
        sale_price=sale_price,
        description=desc,
    )
    db.add(row)
    db.commit()
    return RedirectResponse(url="/sales/material?msg=saved", status_code=303)
