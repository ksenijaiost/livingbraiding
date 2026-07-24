"""Полная выгрузка списка клиентов в CSV (разделитель «;», UTF-8 BOM) для Excel."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.client_validation import client_age_group_label
from app.db.models import Client, Visit
from app.display_time import format_naive_utc_datetime, get_display_timezone


def _cell(v: Any, *, tz_name: str | None = None) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, datetime):
        if tz_name:
            return format_naive_utc_datetime(v, tz_name, "%Y-%m-%d %H:%M:%S")
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def build_all_clients_csv_bytes(db: Session) -> bytes:
    """Все клиенты (без лимита списка на экране), одна таблица."""
    tz = get_display_timezone(db)
    clients = list(
        db.scalars(
            select(Client)
            .options(joinedload(Client.updated_by_user))
            .order_by(Client.id.asc())
        ).unique().all()
    )

    cnt_rows = db.execute(
        select(Visit.client_id, func.count(Visit.id))
        .where(Visit.is_cancelled.is_(False))
        .group_by(Visit.client_id)
    ).all()
    visit_counts: dict[int, int] = {int(cid): int(n) for cid, n in cnt_rows if cid is not None}

    buf = io.StringIO()
    wr = csv.writer(buf, delimiter=";")
    wr.writerow(
        [
            "ID",
            "Имя",
            "Телефон",
            "Telegram",
            "VK",
            "Instagram",
            "Прочий контакт",
            "Возрастная группа",
            "Источник",
            "Источник (другое)",
            "Комментарий",
            "Подтверждён",
            "День рождения",
            "Месяц рождения",
            "Год рождения",
            "Кто завёл (подпись)",
            "Обновлено",
            "Кто обновил",
            "Фото 1 (путь)",
            "Фото 2 (путь)",
            "Число визитов (не аннулированных)",
        ]
    )
    for c in clients:
        updater = getattr(c, "updated_by_user", None)
        updater_name = (updater.display_name or updater.username) if updater else ""
        wr.writerow(
            [
                _cell(c.id),
                _cell(c.name),
                _cell(c.phone),
                _cell(c.telegram),
                _cell(c.vk),
                _cell(c.instagram),
                _cell(c.other_contact),
                client_age_group_label(c.age_group),
                _cell(c.source),
                _cell(c.source_other),
                _cell(c.comment),
                _cell(c.is_confirmed),
                _cell(c.birth_day),
                _cell(c.birth_month),
                _cell(c.birth_year),
                _cell(c.created_by_label),
                _cell(c.updated_at, tz_name=tz),
                updater_name,
                _cell(c.photo_1),
                _cell(c.photo_2),
                str(visit_counts.get(int(c.id), 0)),
            ]
        )
    return buf.getvalue().encode("utf-8-sig")
