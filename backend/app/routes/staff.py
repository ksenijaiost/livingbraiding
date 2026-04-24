from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.audit import FieldChange, diff_fields, write_audit_rows
from app.auth import AuthUser, canonical_staff_phone, require_role
from app.db.models import (
    MasterLevel,
    User,
    UserAuditLog,
    UserRole,
    UserRoleAssignment,
)
from app.db.session import get_db
from app.forms_parse import parse_bool
from app.security import hash_password
from app.user_roles import (
    get_roles_for_user,
    max_user_role,
    set_user_roles,
    user_has_role,
)
from app.webui import templates, ctx as _ctx


router = APIRouter()


_ROLE_FORM_KEYS = (
    (UserRole.ADMIN_SUPER, "role_admin_super"),
    (UserRole.ADMIN, "role_admin"),
    (UserRole.MASTER, "role_master"),
    (UserRole.TECHSPEC, "role_techspec"),
)


def _roles_from_staff_form(form: Any) -> list[UserRole]:
    roles: list[UserRole] = []
    for role, key in _ROLE_FORM_KEYS:
        v = form.get(key)
        if v is None or isinstance(v, UploadFile):
            continue
        if parse_bool(v.decode() if isinstance(v, (bytes, bytearray)) else v):
            roles.append(role)
    return roles


def _parse_master_level_from_form(form: Any, roles: list[UserRole]) -> MasterLevel | None:
    if UserRole.MASTER not in roles:
        return None
    raw = str(form.get("master_level") or "").strip().upper()
    try:
        return MasterLevel(raw)
    except ValueError:
        return MasterLevel.JUNIOR


def _count_other_active_superadmins(db: Session, exclude_user_id: int) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(User)
            .join(UserRoleAssignment, UserRoleAssignment.user_id == User.id)
            .where(
                User.id != exclude_user_id,
                User.is_active.is_(True),
                UserRoleAssignment.role == UserRole.ADMIN_SUPER,
            )
        )
        or 0
    )


def _staff_phone_validated(db: Session, raw: str, *, exclude_user_id: int | None) -> tuple[str | None, str | None]:
    if not (raw or "").strip():
        return None, None
    canon = canonical_staff_phone(raw)
    if canon is None:
        return None, "Телефон: не менее 10 цифр (допускаются +, пробелы, скобки — сохраняются только цифры)."
    q = select(User.id).where(User.phone == canon)
    if exclude_user_id is not None:
        q = q.where(User.id != exclude_user_id)
    if db.scalar(q.limit(1)):
        return None, "Этот номер уже привязан к другому сотруднику."
    return canon, None


def _roles_audit_summary(db: Session, user_id: int) -> str:
    return ",".join(sorted(r.value for r in get_roles_for_user(db, user_id)))


@router.get("/admin/settings/staff", response_class=HTMLResponse)
def admin_settings_staff_list(
    request: Request,
    msg: str | None = None,
    err: str | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    users = list(
        db.scalars(select(User).options(selectinload(User.role_assignments)).order_by(User.is_active.desc(), User.username.asc())).all()
    )
    rows = [{"user": u, "roles": get_roles_for_user(db, u.id)} for u in users]
    return templates.TemplateResponse("admin_settings_staff.html", _ctx(request, current_user=current_user, rows=rows, msg=msg, err=err))


@router.get("/admin/settings/staff/new", response_class=HTMLResponse)
def admin_settings_staff_new_get(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
):
    return templates.TemplateResponse("admin_settings_staff_form.html", _ctx(request, current_user=current_user, is_new=True, user=None, error=None))


@router.post("/admin/settings/staff/new", response_class=HTMLResponse)
async def admin_settings_staff_new_post(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    form = await request.form()
    username = str(form.get("username") or "").strip().lower()
    display_name = str(form.get("display_name") or "").strip()
    password = str(form.get("password") or "")
    roles = _roles_from_staff_form(form)
    if not roles:
        return templates.TemplateResponse(
            "admin_settings_staff_form.html",
            _ctx(request, current_user=current_user, is_new=True, user=None, error="Отметьте хотя бы одну роль."),
            status_code=400,
        )
    if not re.fullmatch(r"[a-z0-9_]{2,50}", username):
        return templates.TemplateResponse(
            "admin_settings_staff_form.html",
            _ctx(request, current_user=current_user, is_new=True, user=None, error="Логин: 2–50 символов, латиница, цифры и подчёркивание."),
            status_code=400,
        )
    if not display_name:
        return templates.TemplateResponse(
            "admin_settings_staff_form.html",
            _ctx(request, current_user=current_user, is_new=True, user=None, error="Укажите отображаемое имя."),
            status_code=400,
        )
    if len(password) < 6:
        return templates.TemplateResponse(
            "admin_settings_staff_form.html",
            _ctx(request, current_user=current_user, is_new=True, user=None, error="Пароль не короче 6 символов."),
            status_code=400,
        )
    if db.scalar(select(User.id).where(User.username == username).limit(1)):
        return templates.TemplateResponse(
            "admin_settings_staff_form.html",
            _ctx(request, current_user=current_user, is_new=True, user=None, error="Такой логин уже занят."),
            status_code=400,
        )
    phone_raw = str(form.get("phone") or "")
    phone_canon, phone_err = _staff_phone_validated(db, phone_raw, exclude_user_id=None)
    if phone_err:
        return templates.TemplateResponse(
            "admin_settings_staff_form.html",
            _ctx(request, current_user=current_user, is_new=True, user=None, error=phone_err),
            status_code=400,
        )
    ml = _parse_master_level_from_form(form, roles)
    u = User(
        username=username,
        display_name=display_name,
        role=max_user_role(roles),
        password_hash=hash_password(password),
        is_active=True,
        master_level=ml,
        phone=phone_canon,
    )
    db.add(u)
    db.flush()
    set_user_roles(db, u, roles)
    rs = ",".join(sorted(r.value for r in roles))
    write_audit_rows(
        db,
        log_model=UserAuditLog,
        entity_field="user_id",
        entity_id=u.id,
        changed_by_user_id=current_user.id,
        changes=[
            FieldChange("created", None, "да"),
            FieldChange("username", None, u.username),
            FieldChange("display_name", None, u.display_name),
            FieldChange("phone", None, u.phone),
            FieldChange("roles", None, rs),
            FieldChange("master_level", None, u.master_level.value if u.master_level else None),
        ],
    )
    db.commit()
    return RedirectResponse(url="/admin/settings/staff?msg=created", status_code=303)


@router.get("/admin/settings/staff/{user_id}/edit", response_class=HTMLResponse)
def admin_settings_staff_edit_get(
    user_id: int,
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    u = db.scalar(select(User).options(selectinload(User.role_assignments)).where(User.id == user_id))
    if not u:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    roles = get_roles_for_user(db, u.id)
    audit_rows = list(
        db.scalars(
            select(UserAuditLog)
            .options(selectinload(UserAuditLog.changed_by_user))
            .where(UserAuditLog.user_id == user_id)
            .order_by(UserAuditLog.changed_at.desc(), UserAuditLog.id.desc())
            .limit(100)
        ).all()
    )
    return templates.TemplateResponse(
        "admin_settings_staff_form.html",
        _ctx(request, current_user=current_user, is_new=False, user=u, roles=roles, error=None, audit_rows=audit_rows),
    )


@router.post("/admin/settings/staff/{user_id}/edit", response_class=HTMLResponse)
async def admin_settings_staff_edit_post(
    user_id: int,
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    u = db.scalar(select(User).options(selectinload(User.role_assignments)).where(User.id == user_id))
    if not u:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    form = await request.form()
    display_name = str(form.get("display_name") or "").strip()
    new_password = str(form.get("new_password") or "")
    roles = _roles_from_staff_form(form)
    if u.id == current_user.id:
        new_active = True
    else:
        new_active = parse_bool(form.get("is_active"))

    def _form_err(msg: str):
        audit_rows = list(
            db.scalars(
                select(UserAuditLog)
                .options(selectinload(UserAuditLog.changed_by_user))
                .where(UserAuditLog.user_id == user_id)
                .order_by(UserAuditLog.changed_at.desc(), UserAuditLog.id.desc())
                .limit(100)
            ).all()
        )
        return templates.TemplateResponse(
            "admin_settings_staff_form.html",
            _ctx(
                request,
                current_user=current_user,
                is_new=False,
                user=u,
                roles=get_roles_for_user(db, u.id),
                error=msg,
                audit_rows=audit_rows,
            ),
            status_code=400,
        )

    if not display_name:
        return _form_err("Укажите отображаемое имя.")
    if not roles:
        return _form_err("Отметьте хотя бы одну роль.")
    if u.id == current_user.id:
        if not new_active:
            return _form_err("Нельзя отключить свою учётную запись.")
        if UserRole.ADMIN_SUPER not in roles:
            return _form_err("Нельзя снять с себя роль суперадмина.")

    had_super = user_has_role(db, u.id, UserRole.ADMIN_SUPER)
    if had_super and u.is_active and ((not new_active) or (UserRole.ADMIN_SUPER not in roles)):
        if _count_other_active_superadmins(db, u.id) < 1:
            return _form_err("Должен остаться хотя бы один активный суперадмин. Назначьте роль другому пользователю.")

    if new_password and len(new_password) < 6:
        return _form_err("Новый пароль не короче 6 символов.")

    phone_raw = str(form.get("phone") or "")
    phone_canon, phone_err = _staff_phone_validated(db, phone_raw, exclude_user_id=u.id)
    if phone_err:
        return _form_err(phone_err)

    ml = _parse_master_level_from_form(form, roles)
    before = SimpleNamespace(
        display_name=u.display_name,
        phone=u.phone,
        is_active=u.is_active,
        master_level=u.master_level,
        roles_summary=_roles_audit_summary(db, u.id),
    )
    u.display_name = display_name
    u.phone = phone_canon
    u.is_active = new_active
    u.master_level = ml
    pwd_changed = bool(new_password)
    if pwd_changed:
        u.password_hash = hash_password(new_password)
    set_user_roles(db, u, roles)
    db.flush()
    after = SimpleNamespace(
        display_name=u.display_name,
        phone=u.phone,
        is_active=u.is_active,
        master_level=u.master_level,
        roles_summary=_roles_audit_summary(db, u.id),
    )
    raw_changes = diff_fields(before, after, ("display_name", "phone", "is_active", "master_level", "roles_summary"))
    changes = [FieldChange("roles" if c.field_name == "roles_summary" else c.field_name, c.old_value, c.new_value) for c in raw_changes]
    if pwd_changed:
        changes.append(FieldChange("password", None, "изменён"))
    write_audit_rows(
        db,
        log_model=UserAuditLog,
        entity_field="user_id",
        entity_id=u.id,
        changed_by_user_id=current_user.id,
        changes=changes,
    )
    db.commit()
    return RedirectResponse(url="/admin/settings/staff?msg=saved", status_code=303)

