"""
Суперадмин: поля анкеты в карточке прайса (категория / подкатегория / позиция).
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_role
from app.audit import diff_fields, write_audit_rows
from app.db.models import (
    CategoryQuestionnaireField,
    CategoryQuestionnaireFieldAuditLog,
    Service,
    ServiceAuditLog,
    ServiceCategory,
    ServiceQuestionnaireField,
    ServiceQuestionnaireFieldAuditLog,
    ServiceSubcategory,
    SubcategoryQuestionnaireField,
    SubcategoryQuestionnaireFieldAuditLog,
    UserRole,
)
from app.db.session import get_db
from app.questionnaire_field_validate import NormalizedQuestionnaireField, validate_questionnaire_field_form
from app.questionnaire.reveal import REVEAL_BLOCK_LABELS, REVEAL_BLOCKS
from app.time_utils import utcnow_naive
from app.webui import ctx as _ctx
from app.webui import templates

templates.env.globals["reveal_block_choices"] = [(b, REVEAL_BLOCK_LABELS[b]) for b in REVEAL_BLOCKS]

router = APIRouter(prefix="/admin/catalog", tags=["admin-questionnaire"])

_CATALOG_EDITOR = Depends(require_role(UserRole.ADMIN_SENIOR, UserRole.ADMIN_SUPER))

FIELD_TYPE_CHOICES = [
    ("TEXT", "Однострочный текст"),
    ("NUMBER", "Число"),
    ("TEXTAREA", "Многострочный текст"),
    ("CHECKBOX", "Галочка"),
    ("SELECT", "Выбор из списка"),
]


def _is_checked(raw: object | None) -> bool:
    if raw is None:
        return False
    return str(raw).lower() in ("1", "on", "true", "yes")


def _options_for_textarea(stored: str | None) -> str:
    if not stored or not str(stored).strip():
        return ""
    try:
        parsed = json.loads(stored)
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        return stored


def _next_sort_order_subcat(db: Session, subcategory_id: int) -> int:
    m = db.scalar(
        select(func.coalesce(func.max(SubcategoryQuestionnaireField.sort_order), -1)).where(
            SubcategoryQuestionnaireField.subcategory_id == subcategory_id
        )
    )
    return int(m) + 1


def _next_sort_order_service(db: Session, service_id: int) -> int:
    m = db.scalar(
        select(func.coalesce(func.max(ServiceQuestionnaireField.sort_order), -1)).where(
            ServiceQuestionnaireField.service_id == service_id
        )
    )
    return int(m) + 1


def _category_field_key_exists(db: Session, category_id: int, field_key: str) -> bool:
    return (
        db.scalar(
            select(CategoryQuestionnaireField.id).where(
                CategoryQuestionnaireField.category_id == category_id,
                CategoryQuestionnaireField.field_key == field_key,
            ).limit(1)
        )
        is not None
    )


def _service_field_key_exists(db: Session, subcategory_id: int, field_key: str) -> bool:
    sid_rows = select(Service.id).where(Service.subcategory_id == subcategory_id)
    return (
        db.scalar(
            select(ServiceQuestionnaireField.id).where(
                ServiceQuestionnaireField.service_id.in_(sid_rows),
                ServiceQuestionnaireField.field_key == field_key,
            ).limit(1)
        )
        is not None
    )


def _subcategory_field_key_exists_for_service(db: Session, service: Service, field_key: str) -> bool:
    return (
        db.scalar(
            select(SubcategoryQuestionnaireField.id).where(
                SubcategoryQuestionnaireField.subcategory_id == service.subcategory_id,
                SubcategoryQuestionnaireField.field_key == field_key,
            ).limit(1)
        )
        is not None
    )


def _move_rows_subcat(db: Session, subcategory_id: int, field_id: int, direction: str) -> bool:
    rows = list(
        db.scalars(
            select(SubcategoryQuestionnaireField)
            .where(SubcategoryQuestionnaireField.subcategory_id == subcategory_id)
            .order_by(SubcategoryQuestionnaireField.sort_order, SubcategoryQuestionnaireField.id)
        ).all()
    )
    return _swap_sort(rows, field_id, direction, db)


def _move_rows_service(db: Session, service_id: int, field_id: int, direction: str) -> bool:
    rows = list(
        db.scalars(
            select(ServiceQuestionnaireField)
            .where(ServiceQuestionnaireField.service_id == service_id)
            .order_by(ServiceQuestionnaireField.sort_order, ServiceQuestionnaireField.id)
        ).all()
    )
    return _swap_sort(rows, field_id, direction, db)


def _swap_sort(rows: list, field_id: int, direction: str, db: Session) -> bool:
    idx = next((i for i, r in enumerate(rows) if r.id == field_id), None)
    if idx is None:
        return False
    j = idx - 1 if direction == "up" else idx + 1
    if j < 0 or j >= len(rows):
        return False
    rows[idx], rows[j] = rows[j], rows[idx]
    for i, r in enumerate(rows):
        r.sort_order = i
    return True


def _next_sort_order_category(db: Session, category_id: int) -> int:
    m = db.scalar(
        select(func.coalesce(func.max(CategoryQuestionnaireField.sort_order), -1)).where(
            CategoryQuestionnaireField.category_id == category_id
        )
    )
    return int(m) + 1


def _move_rows_category(db: Session, category_id: int, field_id: int, direction: str) -> bool:
    rows = list(
        db.scalars(
            select(CategoryQuestionnaireField)
            .where(CategoryQuestionnaireField.category_id == category_id)
            .order_by(CategoryQuestionnaireField.sort_order, CategoryQuestionnaireField.id)
        ).all()
    )
    return _swap_sort(rows, field_id, direction, db)


def _any_lower_scope_uses_field_key(db: Session, category_id: int, field_key: str) -> bool:
    sub_ids = select(ServiceSubcategory.id).where(ServiceSubcategory.category_id == category_id)
    if (
        db.scalar(
            select(SubcategoryQuestionnaireField.id).where(
                SubcategoryQuestionnaireField.subcategory_id.in_(sub_ids),
                SubcategoryQuestionnaireField.field_key == field_key,
            ).limit(1)
        )
        is not None
    ):
        return True
    svc_ids = select(Service.id).join(
        ServiceSubcategory, Service.subcategory_id == ServiceSubcategory.id
    ).where(ServiceSubcategory.category_id == category_id)
    return (
        db.scalar(
            select(ServiceQuestionnaireField.id).where(
                ServiceQuestionnaireField.service_id.in_(svc_ids),
                ServiceQuestionnaireField.field_key == field_key,
            ).limit(1)
        )
        is not None
    )


def _apply_normalized_category(row: CategoryQuestionnaireField, n: NormalizedQuestionnaireField) -> None:
    row.field_key = n.field_key
    row.field_type = n.field_type
    row.label = n.label
    row.required = n.required
    row.placeholder = n.placeholder
    row.help_text = n.help_text
    row.options_json = n.options_json
    row.min_value = n.min_value
    row.max_value = n.max_value
    row.visibility_json = n.visibility_json


def _apply_normalized_subcat(row: SubcategoryQuestionnaireField, n: NormalizedQuestionnaireField) -> None:
    row.field_key = n.field_key
    row.field_type = n.field_type
    row.label = n.label
    row.required = n.required
    row.placeholder = n.placeholder
    row.help_text = n.help_text
    row.options_json = n.options_json
    row.min_value = n.min_value
    row.max_value = n.max_value
    row.visibility_json = n.visibility_json


def _apply_normalized_service(row: ServiceQuestionnaireField, n: NormalizedQuestionnaireField) -> None:
    _apply_normalized_subcat(row, n)  # same attribute names


def _reveal_form_values(
    *,
    reveal_block: list[str] | None = None,
    reveal_field_keys: str | None = None,
    visibility_json: str | None = None,
) -> dict[str, object]:
    from app.questionnaire.reveal import reveal_form_prefill

    if reveal_block is not None or reveal_field_keys is not None:
        return {
            "reveal_blocks": list(reveal_block or []),
            "reveal_field_keys": (reveal_field_keys or "").strip(),
        }
    return reveal_form_prefill(visibility_json)


def _field_form_dict(
    *,
    field_key: str,
    field_type: str,
    label: str,
    required: bool,
    placeholder: str | None,
    help_text: str | None,
    options_json: str | None,
    min_value: str | None,
    max_value: str | None,
    reveal_block: list[str] | None = None,
    reveal_field_keys: str | None = None,
    visibility_json: str | None = None,
) -> dict[str, object]:
    d: dict[str, object] = {
        "field_key": field_key,
        "field_type": field_type,
        "label": label,
        "required": required,
        "placeholder": placeholder or "",
        "help_text": help_text or "",
        "options_json": options_json or "",
        "min_value": min_value or "",
        "max_value": max_value or "",
    }
    d.update(
        _reveal_form_values(
            reveal_block=reveal_block,
            reveal_field_keys=reveal_field_keys,
            visibility_json=visibility_json,
        )
    )
    return d


# --- Category fields ---


@router.get("/categories/{category_id}/fields", response_class=HTMLResponse)
def category_fields_list(
    request: Request,
    category_id: int,
    err: str | None = None,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    cat = db.get(ServiceCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    fields = list(
        db.scalars(
            select(CategoryQuestionnaireField)
            .where(CategoryQuestionnaireField.category_id == category_id)
            .order_by(CategoryQuestionnaireField.sort_order, CategoryQuestionnaireField.id)
        ).all()
    )
    return templates.TemplateResponse(
        "admin_questionnaire_fields_list.html",
        _ctx(
            request,
            current_user,
            scope="category",
            category=cat,
            subcategory=None,
            service=None,
            fields=fields,
            err=err,
        ),
    )


@router.get("/categories/{category_id}/fields/new", response_class=HTMLResponse)
def category_field_new_form(
    request: Request,
    category_id: int,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    cat = db.get(ServiceCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    return templates.TemplateResponse(
        "admin_questionnaire_field_form.html",
        _ctx(
            request,
            current_user,
            scope="category",
            category=cat,
            subcategory=None,
            service=None,
            field=None,
            is_new=True,
            field_types=FIELD_TYPE_CHOICES,
            form={},
            errors=[],
        ),
    )


@router.post("/categories/{category_id}/fields/new")
def category_field_new_save(
    category_id: int,
    request: Request,
    field_key: str = Form(...),
    field_type: str = Form(...),
    label: str = Form(...),
    required: str | None = Form(None),
    placeholder: str | None = Form(None),
    help_text: str | None = Form(None),
    options_json: str | None = Form(None),
    min_value: str | None = Form(None),
    max_value: str | None = Form(None),
    reveal_block: list[str] = Form([]),
    reveal_field_keys: str | None = Form(None),
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    cat = db.get(ServiceCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")

    norm, errors = validate_questionnaire_field_form(
        field_key=field_key,
        field_type_raw=field_type,
        label=label,
        required=_is_checked(required),
        placeholder=placeholder,
        help_text=help_text,
        options_raw=options_json,
        min_raw=min_value,
        max_raw=max_value,
        reveal_blocks=reveal_block,
        reveal_field_keys=reveal_field_keys,
    )
    if norm is None:
        return templates.TemplateResponse(
            "admin_questionnaire_field_form.html",
            _ctx(
                request,
                current_user,
                scope="category",
                category=cat,
                subcategory=None,
                service=None,
                field=None,
                is_new=True,
                field_types=FIELD_TYPE_CHOICES,
                form={
                    "field_key": field_key,
                    "field_type": field_type,
                    "label": label,
                    "required": _is_checked(required),
                    "placeholder": placeholder or "",
                    "help_text": help_text or "",
                    "options_json": options_json or "",
                    "min_value": min_value or "",
                    "max_value": max_value or "",
                    "reveal_blocks": list(reveal_block or []),
                    "reveal_field_keys": (reveal_field_keys or "").strip(),
                },
                errors=errors,
            ),
            status_code=422,
        )

    if _category_field_key_exists(db, category_id, norm.field_key):
        errors = [f"Ключ «{norm.field_key}» уже есть в анкете этой категории."]
        return templates.TemplateResponse(
            "admin_questionnaire_field_form.html",
            _ctx(
                request,
                current_user,
                scope="category",
                category=cat,
                subcategory=None,
                service=None,
                field=None,
                is_new=True,
                field_types=FIELD_TYPE_CHOICES,
                form={
                    "field_key": field_key,
                    "field_type": field_type,
                    "label": label,
                    "required": _is_checked(required),
                    "placeholder": placeholder or "",
                    "help_text": help_text or "",
                    "options_json": options_json or "",
                    "min_value": min_value or "",
                    "max_value": max_value or "",
                    "reveal_blocks": list(reveal_block or []),
                    "reveal_field_keys": (reveal_field_keys or "").strip(),
                },
                errors=errors,
            ),
            status_code=422,
        )

    if _any_lower_scope_uses_field_key(db, category_id, norm.field_key):
        errors = [
            f"Ключ «{norm.field_key}» уже занят в анкете подкатегории или услуги этой категории — выберите другой."
        ]
        return templates.TemplateResponse(
            "admin_questionnaire_field_form.html",
            _ctx(
                request,
                current_user,
                scope="category",
                category=cat,
                subcategory=None,
                service=None,
                field=None,
                is_new=True,
                field_types=FIELD_TYPE_CHOICES,
                form={
                    "field_key": field_key,
                    "field_type": field_type,
                    "label": label,
                    "required": _is_checked(required),
                    "placeholder": placeholder or "",
                    "help_text": help_text or "",
                    "options_json": options_json or "",
                    "min_value": min_value or "",
                    "max_value": max_value or "",
                    "reveal_blocks": list(reveal_block or []),
                    "reveal_field_keys": (reveal_field_keys or "").strip(),
                },
                errors=errors,
            ),
            status_code=422,
        )

    _ = norm.as_structure_dict()

    row = CategoryQuestionnaireField(
        category_id=category_id,
        sort_order=_next_sort_order_category(db, category_id),
    )
    _apply_normalized_category(row, norm)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url=f"/admin/catalog/categories/{category_id}/fields?err=duplicate",
            status_code=303,
        )
    return RedirectResponse(url=f"/admin/catalog/categories/{category_id}/fields", status_code=303)


@router.get("/categories/{category_id}/fields/{field_id}/edit", response_class=HTMLResponse)
def category_field_edit_form(
    request: Request,
    category_id: int,
    field_id: int,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    cat = db.get(ServiceCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    field = db.get(CategoryQuestionnaireField, field_id)
    if field is None or field.category_id != category_id:
        raise HTTPException(status_code=404, detail="Поле не найдено")
    form = {
        "field_key": field.field_key,
        "field_type": field.field_type.value,
        "label": field.label,
        "required": field.required,
        "placeholder": field.placeholder or "",
        "help_text": field.help_text or "",
        "options_json": _options_for_textarea(field.options_json),
        "min_value": "" if field.min_value is None else str(field.min_value).replace(".", ","),
        "max_value": "" if field.max_value is None else str(field.max_value).replace(".", ","),
        **_reveal_form_values(visibility_json=field.visibility_json),
    }
    return templates.TemplateResponse(
        "admin_questionnaire_field_form.html",
        _ctx(
            request,
            current_user,
            scope="category",
            category=cat,
            subcategory=None,
            service=None,
            field=field,
            is_new=False,
            field_types=FIELD_TYPE_CHOICES,
            form=form,
            errors=[],
        ),
    )


@router.post("/categories/{category_id}/fields/{field_id}/edit")
def category_field_edit_save(
    category_id: int,
    field_id: int,
    request: Request,
    field_key: str = Form(...),
    field_type: str = Form(...),
    label: str = Form(...),
    required: str | None = Form(None),
    placeholder: str | None = Form(None),
    help_text: str | None = Form(None),
    options_json: str | None = Form(None),
    min_value: str | None = Form(None),
    max_value: str | None = Form(None),
    reveal_block: list[str] = Form([]),
    reveal_field_keys: str | None = Form(None),
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    cat = db.get(ServiceCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    field_inst = db.get(CategoryQuestionnaireField, field_id)
    if field_inst is None or field_inst.category_id != category_id:
        raise HTTPException(status_code=404, detail="Поле не найдено")

    norm, errors = validate_questionnaire_field_form(
        field_key=field_key,
        field_type_raw=field_type,
        label=label,
        required=_is_checked(required),
        placeholder=placeholder,
        help_text=help_text,
        options_raw=options_json,
        min_raw=min_value,
        max_raw=max_value,
        edit_field_key_locked=field_inst.field_key,
        reveal_blocks=reveal_block,
        reveal_field_keys=reveal_field_keys,
    )
    if norm is None:
        return templates.TemplateResponse(
            "admin_questionnaire_field_form.html",
            _ctx(
                request,
                current_user,
                scope="category",
                category=cat,
                subcategory=None,
                service=None,
                field=field_inst,
                is_new=False,
                field_types=FIELD_TYPE_CHOICES,
                form={
                    "field_key": field_inst.field_key,
                    "field_type": field_type,
                    "label": label,
                    "required": _is_checked(required),
                    "placeholder": placeholder or "",
                    "help_text": help_text or "",
                    "options_json": options_json or "",
                    "min_value": min_value or "",
                    "max_value": max_value or "",
                    "reveal_blocks": list(reveal_block or []),
                    "reveal_field_keys": (reveal_field_keys or "").strip(),
                },
                errors=errors,
            ),
            status_code=422,
        )

    before = SimpleNamespace(
        field_type=field_inst.field_type,
        label=field_inst.label,
        required=field_inst.required,
        sort_order=field_inst.sort_order,
        placeholder=field_inst.placeholder,
        help_text=field_inst.help_text,
        options_json=field_inst.options_json,
        min_value=field_inst.min_value,
        max_value=field_inst.max_value,
        visibility_json=field_inst.visibility_json,
    )
    _ = norm.as_structure_dict()
    _apply_normalized_category(field_inst, norm)
    field_inst.updated_at = utcnow_naive()
    field_inst.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=CategoryQuestionnaireFieldAuditLog,
        entity_field="field_id",
        entity_id=field_inst.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(
            before,
            field_inst,
            (
                "field_type",
                "label",
                "required",
                "sort_order",
                "placeholder",
                "help_text",
                "options_json",
                "min_value",
                "max_value",
                "visibility_json",
            ),
        ),
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url=f"/admin/catalog/categories/{category_id}/fields/{field_id}/edit?err=duplicate",
            status_code=303,
        )
    return RedirectResponse(url=f"/admin/catalog/categories/{category_id}/fields", status_code=303)


@router.post("/categories/{category_id}/fields/{field_id}/delete")
def category_field_delete(
    category_id: int,
    field_id: int,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    field = db.get(CategoryQuestionnaireField, field_id)
    if field is None or field.category_id != category_id:
        raise HTTPException(status_code=404, detail="Поле не найдено")
    db.delete(field)
    db.commit()
    return RedirectResponse(url=f"/admin/catalog/categories/{category_id}/fields", status_code=303)


@router.post("/categories/{category_id}/fields/{field_id}/move")
def category_field_move(
    category_id: int,
    field_id: int,
    direction: str = Form(...),
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    d = (direction or "").lower()
    if d not in ("up", "down"):
        raise HTTPException(status_code=400, detail="bad direction")
    if not _move_rows_category(db, category_id, field_id, d):
        pass
    db.commit()
    return RedirectResponse(url=f"/admin/catalog/categories/{category_id}/fields", status_code=303)


# --- Subcategory fields ---


@router.get("/subcategories/{subcategory_id}/fields", response_class=HTMLResponse)
def subcategory_fields_list(
    request: Request,
    subcategory_id: int,
    err: str | None = None,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    sub = db.get(ServiceSubcategory, subcategory_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Подкатегория не найдена")
    cat = db.get(ServiceCategory, sub.category_id)
    fields = list(
        db.scalars(
            select(SubcategoryQuestionnaireField)
            .where(SubcategoryQuestionnaireField.subcategory_id == subcategory_id)
            .order_by(SubcategoryQuestionnaireField.sort_order, SubcategoryQuestionnaireField.id)
        ).all()
    )
    return templates.TemplateResponse(
        "admin_questionnaire_fields_list.html",
        _ctx(
            request,
            current_user,
            scope="subcategory",
            category=cat,
            subcategory=sub,
            service=None,
            fields=fields,
            err=err,
        ),
    )


@router.get("/subcategories/{subcategory_id}/fields/new", response_class=HTMLResponse)
def subcategory_field_new_form(
    request: Request,
    subcategory_id: int,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    sub = db.get(ServiceSubcategory, subcategory_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Подкатегория не найдена")
    cat = db.get(ServiceCategory, sub.category_id)
    return templates.TemplateResponse(
        "admin_questionnaire_field_form.html",
        _ctx(
            request,
            current_user,
            scope="subcategory",
            category=cat,
            subcategory=sub,
            service=None,
            field=None,
            is_new=True,
            field_types=FIELD_TYPE_CHOICES,
            form={},
            errors=[],
        ),
    )


@router.post("/subcategories/{subcategory_id}/fields/new")
def subcategory_field_new_save(
    subcategory_id: int,
    request: Request,
    field_key: str = Form(...),
    field_type: str = Form(...),
    label: str = Form(...),
    required: str | None = Form(None),
    placeholder: str | None = Form(None),
    help_text: str | None = Form(None),
    options_json: str | None = Form(None),
    min_value: str | None = Form(None),
    max_value: str | None = Form(None),
    reveal_block: list[str] = Form([]),
    reveal_field_keys: str | None = Form(None),
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    sub = db.get(ServiceSubcategory, subcategory_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Подкатегория не найдена")

    norm, errors = validate_questionnaire_field_form(
        field_key=field_key,
        field_type_raw=field_type,
        label=label,
        required=_is_checked(required),
        placeholder=placeholder,
        help_text=help_text,
        options_raw=options_json,
        min_raw=min_value,
        max_raw=max_value,
        reveal_blocks=reveal_block,
        reveal_field_keys=reveal_field_keys,
    )
    if norm is None:
        cat = db.get(ServiceCategory, sub.category_id)
        return templates.TemplateResponse(
            "admin_questionnaire_field_form.html",
            _ctx(
                request,
                current_user,
                scope="subcategory",
                category=cat,
                subcategory=sub,
                service=None,
                field=None,
                is_new=True,
                field_types=FIELD_TYPE_CHOICES,
                form={
                    "field_key": field_key,
                    "field_type": field_type,
                    "label": label,
                    "required": _is_checked(required),
                    "placeholder": placeholder or "",
                    "help_text": help_text or "",
                    "options_json": options_json or "",
                    "min_value": min_value or "",
                    "max_value": max_value or "",
                    "reveal_blocks": list(reveal_block or []),
                    "reveal_field_keys": (reveal_field_keys or "").strip(),
                },
                errors=errors,
            ),
            status_code=422,
        )

    if _service_field_key_exists(db, subcategory_id, norm.field_key):
        errors = [
            f"Ключ «{norm.field_key}» уже используется в анкете одной из услуг этой подкатегории — выберите другой."
        ]
        cat = db.get(ServiceCategory, sub.category_id)
        return templates.TemplateResponse(
            "admin_questionnaire_field_form.html",
            _ctx(
                request,
                current_user,
                scope="subcategory",
                category=cat,
                subcategory=sub,
                service=None,
                field=None,
                is_new=True,
                field_types=FIELD_TYPE_CHOICES,
                form={
                    "field_key": field_key,
                    "field_type": field_type,
                    "label": label,
                    "required": _is_checked(required),
                    "placeholder": placeholder or "",
                    "help_text": help_text or "",
                    "options_json": options_json or "",
                    "min_value": min_value or "",
                    "max_value": max_value or "",
                    "reveal_blocks": list(reveal_block or []),
                    "reveal_field_keys": (reveal_field_keys or "").strip(),
                },
                errors=errors,
            ),
            status_code=422,
        )

    if _category_field_key_exists(db, sub.category_id, norm.field_key):
        errors = [
            f"Ключ «{norm.field_key}» уже задан в анкете категории — выберите другой."
        ]
        cat = db.get(ServiceCategory, sub.category_id)
        return templates.TemplateResponse(
            "admin_questionnaire_field_form.html",
            _ctx(
                request,
                current_user,
                scope="subcategory",
                category=cat,
                subcategory=sub,
                service=None,
                field=None,
                is_new=True,
                field_types=FIELD_TYPE_CHOICES,
                form={
                    "field_key": field_key,
                    "field_type": field_type,
                    "label": label,
                    "required": _is_checked(required),
                    "placeholder": placeholder or "",
                    "help_text": help_text or "",
                    "options_json": options_json or "",
                    "min_value": min_value or "",
                    "max_value": max_value or "",
                    "reveal_blocks": list(reveal_block or []),
                    "reveal_field_keys": (reveal_field_keys or "").strip(),
                },
                errors=errors,
            ),
            status_code=422,
        )

    # JSON-структура после валидации (для отладки / расширений)
    _ = norm.as_structure_dict()

    row = SubcategoryQuestionnaireField(
        subcategory_id=subcategory_id,
        sort_order=_next_sort_order_subcat(db, subcategory_id),
    )
    _apply_normalized_subcat(row, norm)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url=f"/admin/catalog/subcategories/{subcategory_id}/fields?err=duplicate",
            status_code=303,
        )
    return RedirectResponse(url=f"/admin/catalog/subcategories/{subcategory_id}/fields", status_code=303)


@router.get("/subcategories/{subcategory_id}/fields/{field_id}/edit", response_class=HTMLResponse)
def subcategory_field_edit_form(
    request: Request,
    subcategory_id: int,
    field_id: int,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    sub = db.get(ServiceSubcategory, subcategory_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Подкатегория не найдена")
    field = db.get(SubcategoryQuestionnaireField, field_id)
    if field is None or field.subcategory_id != subcategory_id:
        raise HTTPException(status_code=404, detail="Поле не найдено")
    cat = db.get(ServiceCategory, sub.category_id)
    form = {
        "field_key": field.field_key,
        "field_type": field.field_type.value,
        "label": field.label,
        "required": field.required,
        "placeholder": field.placeholder or "",
        "help_text": field.help_text or "",
        "options_json": _options_for_textarea(field.options_json),
        "min_value": "" if field.min_value is None else str(field.min_value).replace(".", ","),
        "max_value": "" if field.max_value is None else str(field.max_value).replace(".", ","),
        **_reveal_form_values(visibility_json=field.visibility_json),
    }
    return templates.TemplateResponse(
        "admin_questionnaire_field_form.html",
        _ctx(
            request,
            current_user,
            scope="subcategory",
            category=cat,
            subcategory=sub,
            service=None,
            field=field,
            is_new=False,
            field_types=FIELD_TYPE_CHOICES,
            form=form,
            errors=[],
        ),
    )


@router.post("/subcategories/{subcategory_id}/fields/{field_id}/edit")
def subcategory_field_edit_save(
    subcategory_id: int,
    field_id: int,
    request: Request,
    field_key: str = Form(...),
    field_type: str = Form(...),
    label: str = Form(...),
    required: str | None = Form(None),
    placeholder: str | None = Form(None),
    help_text: str | None = Form(None),
    options_json: str | None = Form(None),
    min_value: str | None = Form(None),
    max_value: str | None = Form(None),
    reveal_block: list[str] = Form([]),
    reveal_field_keys: str | None = Form(None),
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    sub = db.get(ServiceSubcategory, subcategory_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Подкатегория не найдена")
    field = db.get(SubcategoryQuestionnaireField, field_id)
    if field is None or field.subcategory_id != subcategory_id:
        raise HTTPException(status_code=404, detail="Поле не найдено")

    norm, errors = validate_questionnaire_field_form(
        field_key=field_key,
        field_type_raw=field_type,
        label=label,
        required=_is_checked(required),
        placeholder=placeholder,
        help_text=help_text,
        options_raw=options_json,
        min_raw=min_value,
        max_raw=max_value,
        edit_field_key_locked=field.field_key,
        reveal_blocks=reveal_block,
        reveal_field_keys=reveal_field_keys,
    )
    if norm is None:
        cat = db.get(ServiceCategory, sub.category_id)
        return templates.TemplateResponse(
            "admin_questionnaire_field_form.html",
            _ctx(
                request,
                current_user,
                scope="subcategory",
                category=cat,
                subcategory=sub,
                service=None,
                field=field,
                is_new=False,
                field_types=FIELD_TYPE_CHOICES,
                form={
                    "field_key": field.field_key,
                    "field_type": field_type,
                    "label": label,
                    "required": _is_checked(required),
                    "placeholder": placeholder or "",
                    "help_text": help_text or "",
                    "options_json": options_json or "",
                    "min_value": min_value or "",
                    "max_value": max_value or "",
                    "reveal_blocks": list(reveal_block or []),
                    "reveal_field_keys": (reveal_field_keys or "").strip(),
                },
                errors=errors,
            ),
            status_code=422,
        )

    before = SimpleNamespace(
        field_type=field.field_type,
        label=field.label,
        required=field.required,
        sort_order=field.sort_order,
        placeholder=field.placeholder,
        help_text=field.help_text,
        options_json=field.options_json,
        min_value=field.min_value,
        max_value=field.max_value,
        visibility_json=field.visibility_json,
    )
    _ = norm.as_structure_dict()
    _apply_normalized_subcat(field, norm)
    field.updated_at = utcnow_naive()
    field.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=SubcategoryQuestionnaireFieldAuditLog,
        entity_field="field_id",
        entity_id=field.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(
            before,
            field,
            (
                "field_type",
                "label",
                "required",
                "sort_order",
                "placeholder",
                "help_text",
                "options_json",
                "min_value",
                "max_value",
                "visibility_json",
            ),
        ),
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url=f"/admin/catalog/subcategories/{subcategory_id}/fields/{field_id}/edit?err=duplicate",
            status_code=303,
        )
    return RedirectResponse(url=f"/admin/catalog/subcategories/{subcategory_id}/fields", status_code=303)


@router.post("/subcategories/{subcategory_id}/fields/{field_id}/delete")
def subcategory_field_delete(
    subcategory_id: int,
    field_id: int,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    field = db.get(SubcategoryQuestionnaireField, field_id)
    if field is None or field.subcategory_id != subcategory_id:
        raise HTTPException(status_code=404, detail="Поле не найдено")
    db.delete(field)
    db.commit()
    return RedirectResponse(url=f"/admin/catalog/subcategories/{subcategory_id}/fields", status_code=303)


@router.post("/subcategories/{subcategory_id}/fields/{field_id}/move")
def subcategory_field_move(
    subcategory_id: int,
    field_id: int,
    direction: str = Form(...),
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    d = (direction or "").lower()
    if d not in ("up", "down"):
        raise HTTPException(status_code=400, detail="bad direction")
    if not _move_rows_subcat(db, subcategory_id, field_id, d):
        pass
    db.commit()
    return RedirectResponse(url=f"/admin/catalog/subcategories/{subcategory_id}/fields", status_code=303)


# --- Service fields ---


@router.get("/services/{service_id}/fields", response_class=HTMLResponse)
def service_fields_list(
    request: Request,
    service_id: int,
    err: str | None = None,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    svc = db.get(Service, service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail="Услуга не найдена")
    sub = db.get(ServiceSubcategory, svc.subcategory_id)
    cat = db.get(ServiceCategory, sub.category_id) if sub else None
    fields = list(
        db.scalars(
            select(ServiceQuestionnaireField)
            .where(ServiceQuestionnaireField.service_id == service_id)
            .order_by(ServiceQuestionnaireField.sort_order, ServiceQuestionnaireField.id)
        ).all()
    )
    return templates.TemplateResponse(
        "admin_questionnaire_fields_list.html",
        _ctx(
            request,
            current_user,
            scope="service",
            category=cat,
            subcategory=sub,
            service=svc,
            fields=fields,
            err=err,
        ),
    )


@router.get("/services/{service_id}/fields/new", response_class=HTMLResponse)
def service_field_new_form(
    request: Request,
    service_id: int,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    svc = db.get(Service, service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail="Услуга не найдена")
    sub = db.get(ServiceSubcategory, svc.subcategory_id)
    cat = db.get(ServiceCategory, sub.category_id) if sub else None
    return templates.TemplateResponse(
        "admin_questionnaire_field_form.html",
        _ctx(
            request,
            current_user,
            scope="service",
            category=cat,
            subcategory=sub,
            service=svc,
            field=None,
            is_new=True,
            field_types=FIELD_TYPE_CHOICES,
            form={},
            errors=[],
        ),
    )


@router.post("/services/{service_id}/fields/new")
def service_field_new_save(
    service_id: int,
    request: Request,
    field_key: str = Form(...),
    field_type: str = Form(...),
    label: str = Form(...),
    required: str | None = Form(None),
    placeholder: str | None = Form(None),
    help_text: str | None = Form(None),
    options_json: str | None = Form(None),
    min_value: str | None = Form(None),
    max_value: str | None = Form(None),
    reveal_block: list[str] = Form([]),
    reveal_field_keys: str | None = Form(None),
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    svc = db.get(Service, service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail="Услуга не найдена")

    norm, errors = validate_questionnaire_field_form(
        field_key=field_key,
        field_type_raw=field_type,
        label=label,
        required=_is_checked(required),
        placeholder=placeholder,
        help_text=help_text,
        options_raw=options_json,
        min_raw=min_value,
        max_raw=max_value,
        reveal_blocks=reveal_block,
        reveal_field_keys=reveal_field_keys,
    )
    if norm is None:
        sub = db.get(ServiceSubcategory, svc.subcategory_id)
        cat = db.get(ServiceCategory, sub.category_id) if sub else None
        return templates.TemplateResponse(
            "admin_questionnaire_field_form.html",
            _ctx(
                request,
                current_user,
                scope="service",
                category=cat,
                subcategory=sub,
                service=svc,
                field=None,
                is_new=True,
                field_types=FIELD_TYPE_CHOICES,
                form={
                    "field_key": field_key,
                    "field_type": field_type,
                    "label": label,
                    "required": _is_checked(required),
                    "placeholder": placeholder or "",
                    "help_text": help_text or "",
                    "options_json": options_json or "",
                    "min_value": min_value or "",
                    "max_value": max_value or "",
                    "reveal_blocks": list(reveal_block or []),
                    "reveal_field_keys": (reveal_field_keys or "").strip(),
                },
                errors=errors,
            ),
            status_code=422,
        )

    if _subcategory_field_key_exists_for_service(db, svc, norm.field_key):
        errors = [
            f"Ключ «{norm.field_key}» уже занят в общей анкете подкатегории — выберите другой."
        ]
        sub = db.get(ServiceSubcategory, svc.subcategory_id)
        cat = db.get(ServiceCategory, sub.category_id) if sub else None
        return templates.TemplateResponse(
            "admin_questionnaire_field_form.html",
            _ctx(
                request,
                current_user,
                scope="service",
                category=cat,
                subcategory=sub,
                service=svc,
                field=None,
                is_new=True,
                field_types=FIELD_TYPE_CHOICES,
                form={
                    "field_key": field_key,
                    "field_type": field_type,
                    "label": label,
                    "required": _is_checked(required),
                    "placeholder": placeholder or "",
                    "help_text": help_text or "",
                    "options_json": options_json or "",
                    "min_value": min_value or "",
                    "max_value": max_value or "",
                    "reveal_blocks": list(reveal_block or []),
                    "reveal_field_keys": (reveal_field_keys or "").strip(),
                },
                errors=errors,
            ),
            status_code=422,
        )

    sub_for_cat = db.get(ServiceSubcategory, svc.subcategory_id)
    if sub_for_cat and _category_field_key_exists(db, sub_for_cat.category_id, norm.field_key):
        errors = [f"Ключ «{norm.field_key}» уже задан в анкете категории — выберите другой."]
        cat = db.get(ServiceCategory, sub_for_cat.category_id)
        return templates.TemplateResponse(
            "admin_questionnaire_field_form.html",
            _ctx(
                request,
                current_user,
                scope="service",
                category=cat,
                subcategory=sub_for_cat,
                service=svc,
                field=None,
                is_new=True,
                field_types=FIELD_TYPE_CHOICES,
                form={
                    "field_key": field_key,
                    "field_type": field_type,
                    "label": label,
                    "required": _is_checked(required),
                    "placeholder": placeholder or "",
                    "help_text": help_text or "",
                    "options_json": options_json or "",
                    "min_value": min_value or "",
                    "max_value": max_value or "",
                    "reveal_blocks": list(reveal_block or []),
                    "reveal_field_keys": (reveal_field_keys or "").strip(),
                },
                errors=errors,
            ),
            status_code=422,
        )

    _ = norm.as_structure_dict()

    row = ServiceQuestionnaireField(
        service_id=service_id,
        sort_order=_next_sort_order_service(db, service_id),
    )
    _apply_normalized_service(row, norm)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(url=f"/admin/catalog/services/{service_id}/fields?err=duplicate", status_code=303)
    return RedirectResponse(url=f"/admin/catalog/services/{service_id}/fields", status_code=303)


@router.get("/services/{service_id}/fields/{field_id}/edit", response_class=HTMLResponse)
def service_field_edit_form(
    request: Request,
    service_id: int,
    field_id: int,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    svc = db.get(Service, service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail="Услуга не найдена")
    field = db.get(ServiceQuestionnaireField, field_id)
    if field is None or field.service_id != service_id:
        raise HTTPException(status_code=404, detail="Поле не найдено")
    sub = db.get(ServiceSubcategory, svc.subcategory_id)
    cat = db.get(ServiceCategory, sub.category_id) if sub else None
    form = {
        "field_key": field.field_key,
        "field_type": field.field_type.value,
        "label": field.label,
        "required": field.required,
        "placeholder": field.placeholder or "",
        "help_text": field.help_text or "",
        "options_json": _options_for_textarea(field.options_json),
        "min_value": "" if field.min_value is None else str(field.min_value).replace(".", ","),
        "max_value": "" if field.max_value is None else str(field.max_value).replace(".", ","),
        **_reveal_form_values(visibility_json=field.visibility_json),
    }
    return templates.TemplateResponse(
        "admin_questionnaire_field_form.html",
        _ctx(
            request,
            current_user,
            scope="service",
            category=cat,
            subcategory=sub,
            service=svc,
            field=field,
            is_new=False,
            field_types=FIELD_TYPE_CHOICES,
            form=form,
            errors=[],
        ),
    )


@router.post("/services/{service_id}/fields/{field_id}/edit")
def service_field_edit_save(
    service_id: int,
    field_id: int,
    request: Request,
    field_key: str = Form(...),
    field_type: str = Form(...),
    label: str = Form(...),
    required: str | None = Form(None),
    placeholder: str | None = Form(None),
    help_text: str | None = Form(None),
    options_json: str | None = Form(None),
    min_value: str | None = Form(None),
    max_value: str | None = Form(None),
    reveal_block: list[str] = Form([]),
    reveal_field_keys: str | None = Form(None),
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    svc = db.get(Service, service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail="Услуга не найдена")
    field_inst = db.get(ServiceQuestionnaireField, field_id)
    if field_inst is None or field_inst.service_id != service_id:
        raise HTTPException(status_code=404, detail="Поле не найдено")

    norm, errors = validate_questionnaire_field_form(
        field_key=field_key,
        field_type_raw=field_type,
        label=label,
        required=_is_checked(required),
        placeholder=placeholder,
        help_text=help_text,
        options_raw=options_json,
        min_raw=min_value,
        max_raw=max_value,
        edit_field_key_locked=field_inst.field_key,
        reveal_blocks=reveal_block,
        reveal_field_keys=reveal_field_keys,
    )
    if norm is None:
        sub = db.get(ServiceSubcategory, svc.subcategory_id)
        cat = db.get(ServiceCategory, sub.category_id) if sub else None
        return templates.TemplateResponse(
            "admin_questionnaire_field_form.html",
            _ctx(
                request,
                current_user,
                scope="service",
                category=cat,
                subcategory=sub,
                service=svc,
                field=field_inst,
                is_new=False,
                field_types=FIELD_TYPE_CHOICES,
                form={
                    "field_key": field_inst.field_key,
                    "field_type": field_type,
                    "label": label,
                    "required": _is_checked(required),
                    "placeholder": placeholder or "",
                    "help_text": help_text or "",
                    "options_json": options_json or "",
                    "min_value": min_value or "",
                    "max_value": max_value or "",
                    "reveal_blocks": list(reveal_block or []),
                    "reveal_field_keys": (reveal_field_keys or "").strip(),
                },
                errors=errors,
            ),
            status_code=422,
        )

    before = SimpleNamespace(
        field_type=field_inst.field_type,
        label=field_inst.label,
        required=field_inst.required,
        sort_order=field_inst.sort_order,
        placeholder=field_inst.placeholder,
        help_text=field_inst.help_text,
        options_json=field_inst.options_json,
        min_value=field_inst.min_value,
        max_value=field_inst.max_value,
        visibility_json=field_inst.visibility_json,
    )
    _ = norm.as_structure_dict()
    _apply_normalized_service(field_inst, norm)
    field_inst.updated_at = utcnow_naive()
    field_inst.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=ServiceQuestionnaireFieldAuditLog,
        entity_field="field_id",
        entity_id=field_inst.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(
            before,
            field_inst,
            (
                "field_type",
                "label",
                "required",
                "sort_order",
                "placeholder",
                "help_text",
                "options_json",
                "min_value",
                "max_value",
                "visibility_json",
            ),
        ),
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url=f"/admin/catalog/services/{service_id}/fields/{field_id}/edit?err=duplicate",
            status_code=303,
        )
    return RedirectResponse(url=f"/admin/catalog/services/{service_id}/fields", status_code=303)


@router.post("/services/{service_id}/fields/{field_id}/delete")
def service_field_delete(
    service_id: int,
    field_id: int,
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    field = db.get(ServiceQuestionnaireField, field_id)
    if field is None or field.service_id != service_id:
        raise HTTPException(status_code=404, detail="Поле не найдено")
    db.delete(field)
    db.commit()
    return RedirectResponse(url=f"/admin/catalog/services/{service_id}/fields", status_code=303)


@router.post("/services/{service_id}/fields/{field_id}/move")
def service_field_move(
    service_id: int,
    field_id: int,
    direction: str = Form(...),
    current_user: AuthUser = _CATALOG_EDITOR,
    db: Session = Depends(get_db),
):
    d = (direction or "").lower()
    if d not in ("up", "down"):
        raise HTTPException(status_code=400, detail="bad direction")
    if not _move_rows_service(db, service_id, field_id, d):
        pass
    db.commit()
    return RedirectResponse(url=f"/admin/catalog/services/{service_id}/fields", status_code=303)
