"""Имена мастеров в списках визитов и работ."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import User, Visit, VisitService, WorkForInventory


def _master_ids_from_visit(v: Visit) -> set[int]:
    ids: set[int] = set()
    for vm in v.masters or []:
        if vm.master_id:
            ids.add(int(vm.master_id))
    if v.mix_bonus_master_id:
        ids.add(int(v.mix_bonus_master_id))
    for vs in v.services or []:
        if vs.is_cancelled:
            continue
        if vs.mix_bonus_master_id:
            ids.add(int(vs.mix_bonus_master_id))
        for psm in vs.masters or []:
            if psm.master_id:
                ids.add(int(psm.master_id))
    return ids


def visit_list_master_labels(db: Session, visits: list[Visit]) -> dict[int, str]:
    if not visits:
        return {}
    per_visit: dict[int, set[int]] = {}
    all_user_ids: set[int] = set()
    for v in visits:
        mids = _master_ids_from_visit(v)
        per_visit[int(v.id)] = mids
        all_user_ids.update(mids)

    name_by_id: dict[int, str] = {}
    if all_user_ids:
        for u in db.scalars(select(User).where(User.id.in_(all_user_ids))).all():
            name_by_id[int(u.id)] = (u.display_name or u.username or f"#{u.id}").strip() or f"#{u.id}"

    return {
        vid: ", ".join(sorted([name_by_id.get(mid, f"#{mid}") for mid in mids], key=str.casefold))
        for vid, mids in per_visit.items()
    }


def work_list_master_labels(rows: list[WorkForInventory]) -> dict[int, str]:
    out: dict[int, str] = {}
    for w in rows:
        names = sorted(
            [
                (s.user.display_name or s.user.username or f"#{s.user_id}").strip()
                for s in (w.staff_rows or [])
                if s.user_id and s.user
            ],
            key=str.casefold,
        )
        out[int(w.id)] = ", ".join(names)
    return out
