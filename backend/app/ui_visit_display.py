"""
Человекочитаемые подписи для админки (без имён Enum в интерфейсе).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import MixComplexity, MixSource, User, Visit, VisitClientType, VisitMastersScope, VisitPriceType, VisitService
from app.payroll_fund import money_q2


def ru_client_type(ct: VisitClientType | str) -> str:
    v = ct.value if isinstance(ct, VisitClientType) else str(ct)
    return {
        "NEW": "Новый",
        "RETURNING": "Повторный",
        "SELF": "Свой",
    }.get(v, v)


def ru_price_type(pt: VisitPriceType | str) -> str:
    v = pt.value if isinstance(pt, VisitPriceType) else str(pt)
    return {
        "CLIENT": "Клиент",
        "MODEL": "Модель",
    }.get(v, v)


def ru_mix_source(ms: MixSource | str | None) -> str:
    if ms is None:
        return "—"
    v = ms.value if isinstance(ms, MixSource) else str(ms)
    return {
        "FROM_STOCK": "Из наличия",
        "NO_MIX": "Без смешки",
        "SELF_MIXED": "Сама мешала",
    }.get(v, v)


def ru_mix_complexity(c: MixComplexity | str | None) -> str:
    if c is None:
        return "—"
    v = c.value if isinstance(c, MixComplexity) else str(c)
    v = {
        "SIMPLE": "STANDARD",
        "MEDIUM": "KANEK",
        "HARD": "THERMO",
    }.get(str(v).strip().upper(), str(v).strip().upper())
    return {
        "LIGHT": "лёгкая (домешивание)",
        "STANDARD": "стандарт",
        "KANEK": "сложная канекалон (омбре, мелирование)",
        "THERMO": "сложная термо",
        "LENGTH": "сложная длина",
    }.get(v, v)


def ru_kit_kind(kind: str) -> str:
    return {
        "STOCK": "Из наличия",
        "NEW": "Новый",
        "OWN": "Свой",
    }.get(kind, kind)


def ru_own_origin(o: str) -> str:
    return {"STUDIO": "Нашей студии", "FOREIGN": "Чужой"}.get(o, o)


def visit_primary_service_name(visit: Visit) -> str:
    if visit.services:
        return visit.services[0].service_name
    return "—"


def kit_usages_empty_explanation() -> str:
    return (
        "Со склада ничего не списывалось: выбран новый комплект или свой комплект "
        "без дополнительных заготовок из наличия."
    )


def _format_card_scalar(v: Any) -> str:
    """Для карточки визита: целые числа без «.0» (JSON часто отдаёт float)."""
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "Да" if v else "Нет"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    if isinstance(v, str):
        t = v.strip()
        if not t:
            return "—"
        try:
            f = float(t.replace(",", "."))
            if f == int(f):
                return str(int(f))
        except ValueError:
            pass
        return v
    return str(v)


def _append_thermo_template_numbers(blocks: list[tuple[str, str]], t: dict, *, title: str) -> None:
    if title:
        blocks.append((title, "—"))
    blocks.append(("Средний вес пряди", _format_card_scalar(t.get("strand_weight_avg", 0))))
    blocks.append(("1 ряд", _format_card_scalar(t.get("row_1", 0))))
    blocks.append(("2 ряд", _format_card_scalar(t.get("row_2", 0))))
    blocks.append(("3 ряд", _format_card_scalar(t.get("row_3", 0))))
    ot = t.get("other_rows_text") or ""
    if ot:
        blocks.append(("Другие ряды", str(ot)))
    blocks.append(("Виски", _format_card_scalar(t.get("temples", 0))))
    blocks.append(("Треугольники", _format_card_scalar(t.get("triangles", 0))))
    blocks.append(("Птичка", _format_card_scalar(t.get("bird", 0))))
    blocks.append(("Квадрат", _format_card_scalar(t.get("square", 0))))
    cmt = t.get("comment") or ""
    if cmt:
        blocks.append(("Комментарий (шаблон)", str(cmt)))


def _append_thermo_visit_blocks(blocks: list[tuple[str, str]], thermo: Any) -> None:
    if not isinstance(thermo, dict) or not thermo:
        return
    blocks.append(("Кудри (материал)", thermo.get("curls_material") or "—"))
    blocks.append(("Длина материала", thermo.get("material_length") or "—"))
    blocks.append(("Оттенок", thermo.get("shade") or "—"))
    blocks.append(("Общее количество баз", _format_card_scalar(thermo.get("bases_total", 0))))
    blocks.append(
        (
            "Вес материала (всего со страховками)",
            _format_card_scalar(thermo.get("weight_with_margin", 0)),
        )
    )
    mode = thermo.get("template_mode")
    blocks.append(("Какой шаблон", "Новый" if mode == "NEW" else "Старый" if mode == "OLD" else "—"))
    if mode == "OLD":
        oid = thermo.get("old_template_id")
        if oid:
            blocks.append(("Выбран сохранённый шаблон №", str(oid)))
        ac = thermo.get("algorithm_changes")
        if ac:
            blocks.append(("Изменения в алгоритме плетения", str(ac)))
        snap = thermo.get("saved_template_snapshot")
        if isinstance(snap, dict):
            _append_thermo_template_numbers(blocks, snap, title="Параметры выбранного шаблона")
    elif mode == "NEW":
        filled = thermo.get("filled_template")
        if isinstance(filled, dict):
            _append_thermo_template_numbers(blocks, filled, title="Новый шаблон")


def build_service_human_display(vs: VisitService) -> dict[str, Any]:
    """Блоки для карточки услуги без сырого JSON."""
    blocks: list[tuple[str, str]] = [
        ("Категория", vs.category_name),
        ("Подкатегория", vs.subcategory_name),
        ("Услуга", vs.service_name),
    ]
    if not vs.details_json:
        catalog_line = f"{vs.category_name} / {vs.subcategory_name} / {vs.service_name}"
        return {"blocks": blocks, "catalog_line": catalog_line, "detail_blocks": []}

    try:
        data = json.loads(vs.details_json)
    except (json.JSONDecodeError, TypeError):
        err_blocks = blocks + [("Детали", "не удалось разобрать")]
        catalog_line = f"{vs.category_name} / {vs.subcategory_name} / {vs.service_name}"
        return {"blocks": err_blocks, "catalog_line": catalog_line, "detail_blocks": err_blocks[3:]}

    sf = data.get("service_fields") or {}
    if "bases_count" in sf:
        blocks.append(("Количество баз", _format_card_scalar(sf["bases_count"])))
    if "blanks_count" in sf:
        blocks.append(("Количество заготовок (в работе)", _format_card_scalar(sf["blanks_count"])))
    com = sf.get("service_comment")
    if com:
        blocks.append(("Комментарий по услуге", str(com)))

    ans = data.get("answers") or {}
    labels = data.get("answer_labels") or {}
    displays = data.get("answer_display") or {}
    if isinstance(ans, dict) and ans:
        for ak in sorted(ans.keys()):
            av = ans[ak]
            if isinstance(displays, dict) and ak in displays:
                avs = displays[ak]
            elif isinstance(av, bool):
                avs = "Да" if av else "Нет"
            elif av is None:
                avs = "—"
            else:
                avs = _format_card_scalar(av)
            hdr = labels.get(ak) if isinstance(labels, dict) else None
            if hdr:
                blocks.append((hdr, avs))
            else:
                blocks.append((f"Вопрос ({ak})", avs))

    _append_thermo_visit_blocks(blocks, data.get("thermo"))

    kit = data.get("kit")
    if kit and isinstance(kit, dict):
        kind = kit.get("kind") or "?"
        blocks.append(("Тип комплекта", ru_kit_kind(kind)))

        if kind == "STOCK":
            stocks = kit.get("from_stocks")
            lines: list[dict] = []
            if isinstance(stocks, list) and stocks:
                for it in stocks:
                    if isinstance(it, dict):
                        lines.append(it)
            if not lines:
                fs0 = kit.get("from_stock")
                if isinstance(fs0, dict):
                    lines = [fs0]
            for idx, fs in enumerate(lines, start=1):
                sku = fs.get("sku", "—")
                ent = fs.get("use_entire_kit")
                bu = fs.get("blanks_used", 0)
                prefix = "Со склада" if len(lines) == 1 else f"Со склада ({idx})"
                if ent:
                    blocks.append((prefix, f"арт. {sku}, весь комплект"))
                else:
                    blocks.append((prefix, f"арт. {sku}, заготовок: {_format_card_scalar(bu)}"))
        elif kind == "NEW":
            nk = kit.get("new_kit") or {}
            blocks.append(("Новый комплект", nk.get("title") or "—"))
            if nk.get("description"):
                blocks.append(("Описание комплекта", str(nk["description"])))
            bt = nk.get("blanks_total")
            blocks.append(
                ("Всего заготовок в комплекте", "—" if bt is None else _format_card_scalar(bt))
            )
            if nk.get("sku"):
                blocks.append(("Артикул", str(nk["sku"])))
            blocks.append(
                (
                    "Изготовил тот же мастер",
                    "Да" if nk.get("made_by_self") else "Нет",
                )
            )
            if nk.get("notes"):
                blocks.append(("Заметки", str(nk["notes"])))
        elif kind == "OWN":
            ow = kit.get("own") or {}
            blocks.append(("Происхождение", ru_own_origin(str(ow.get("origin", "")))))
            blocks.append(("Коррекция", "Да" if ow.get("correction") else "Нет"))
            cd = ow.get("correction_details")
            if ow.get("correction") and isinstance(cd, dict):
                blocks.append(("Корр.: стрижка (шт)", _format_card_scalar(cd.get("trim_qty", 0))))
                hh = cd.get("hourly_hours", 0)
                blocks.append(("Корр.: почасовая коррекция", _format_card_scalar(hh) + " ч"))
                if cd.get("kit_description"):
                    blocks.append(("Корр.: описание комплекта", str(cd.get("kit_description"))))
                if cd.get("kit_blanks_count") is not None:
                    blocks.append(
                        ("Корр.: заготовок в комплекте (учёт)", _format_card_scalar(cd.get("kit_blanks_count")))
                    )
                if cd.get("use_custom_amount") and cd.get("custom_amount") is not None:
                    blocks.append(
                        ("Корр.: своя сумма с клиента", _format_card_scalar(cd.get("custom_amount")) + " ₽")
                    )
                blocks.append(("Корр.: стирка", "Да" if cd.get("wash") else "Нет"))
                blocks.append(("Корр.: отпаривание", "Да" if cd.get("steam") else "Нет"))
                blocks.append(("Корр.: одевание на круг", "Да" if cd.get("circle") else "Нет"))
                if cd.get("master_id"):
                    blocks.append(("Корр.: мастер", f"ID {cd.get('master_id')}"))
                if cd.get("dread_qty"):
                    blocks.append(("Корр.: (архив) дреды (шт)", _format_card_scalar(cd.get("dread_qty"))))
                if cd.get("curl_qty"):
                    blocks.append(("Корр.: (архив) кудри (шт)", _format_card_scalar(cd.get("curl_qty"))))
            blocks.append(("Дополнительные заготовки", "Да" if ow.get("extra_blanks") else "Нет"))
            ex = ow.get("extra")
            if ex and isinstance(ex, dict):
                src = ex.get("source")
                if src == "STOCK":
                    fs = ex.get("from_stock") or {}
                    sku = fs.get("sku", "—")
                    if fs.get("use_entire_kit"):
                        blocks.append(("Доп. со склада", f"арт. {sku}, весь комплект"))
                    else:
                        blocks.append(
                            (
                                "Доп. со склада",
                                f"арт. {sku}, шт: {_format_card_scalar(fs.get('blanks_used', 0))}",
                            )
                        )
                elif src == "NEW":
                    nk = ex.get("new_kit") or {}
                    blocks.append(("Доп. новые заготовки", nk.get("title") or "—"))
                    if nk.get("blanks_total") is not None:
                        blocks.append(("Количество доп. заготовок", _format_card_scalar(nk["blanks_total"])))

    corr_amt = getattr(vs, "correction_master_amount", 0) or 0
    if float(corr_amt) > 0:
        blocks.append(("Корр.: ЗП мастера", _format_card_scalar(corr_amt) + " ₽"))

    catalog_line = f"{vs.category_name} / {vs.subcategory_name} / {vs.service_name}"
    detail_blocks = blocks[3:]
    return {"blocks": blocks, "catalog_line": catalog_line, "detail_blocks": detail_blocks}


def visit_services_catalog_line(visit: Visit) -> str:
    """Строка для списка визитов: категория / подкатегория / услуга (все строки визита)."""
    svcs = sorted(visit.services or [], key=lambda s: s.id)
    if not svcs:
        return "—"
    return "; ".join(
        f"{vs.category_name} / {vs.subcategory_name} / {vs.service_name}" for vs in svcs
    )


@dataclass(frozen=True)
class VisitMasterPayRow:
    master_id: int
    master_name: str
    pool_share: float
    mix_bonus: float
    correction_bonus: float
    hourly_help: float
    total: float


def _master_display_name(user: User | None, master_id: int, db: Session | None) -> str:
    if user is not None:
        label = (user.display_name or user.username or "").strip()
        if label:
            return label
    if db is not None:
        u = db.get(User, master_id)
        if u is not None:
            label = (u.display_name or u.username or "").strip()
            if label:
                return label
    return f"ID {master_id}"


def visit_masters_fund_by_master(visit: Visit) -> dict[int, float]:
    """ЗП в фонды по master_id (доля пула + коррекция + почасовая помощь), как в карточке и проводках.

    Учитывает masters_scope: PER_SERVICE → доли с строк услуг, иначе — visit.masters.
    """
    from app.hourly_help import hourly_help_rows_from_visit

    by_master: dict[int, float] = {}

    def add(mid: int, amount: float) -> None:
        amt = money_q2(float(amount or 0))
        if amt <= 0:
            return
        by_master[mid] = money_q2(by_master.get(mid, 0.0) + amt)

    active_services = [vs for vs in (visit.services or []) if not vs.is_cancelled]
    if active_services:
        for vs in active_services:
            pool = float(vs.masters_pool or 0)
            if visit.masters_scope == VisitMastersScope.PER_SERVICE:
                master_rows = vs.masters or []
            else:
                master_rows = visit.masters or []
            for m in master_rows:
                add(int(m.master_id), pool * float(m.percent or 0) / 100.0)
            if getattr(vs, "correction_master_id", None):
                add(int(vs.correction_master_id), float(getattr(vs, "correction_master_amount", 0) or 0))
    else:
        pool = float(visit.masters_pool or 0)
        for m in visit.masters or []:
            add(int(m.master_id), pool * float(m.percent or 0) / 100.0)
        if getattr(visit, "correction_master_id", None):
            add(int(visit.correction_master_id), float(getattr(visit, "correction_master_amount", 0) or 0))
    for row in hourly_help_rows_from_visit(visit):
        add(int(row.master_id), float(row.amount or 0))
    return by_master


def visit_masters_fund_total(visit: Visit) -> float:
    return money_q2(sum(visit_masters_fund_by_master(visit).values()))


def build_visit_master_pay_rows(visit: Visit, db: Session | None = None) -> list[VisitMasterPayRow]:
    """ЗП каждого мастера по визиту: доля пула, коррекция и почасовая помощь."""
    from app.hourly_help import hourly_help_rows_from_visit

    active_services = [vs for vs in (visit.services or []) if not vs.is_cancelled]
    pool_by_master: dict[int, float] = {}
    correction_by_master: dict[int, float] = {}
    help_by_master: dict[int, float] = {}
    names: dict[int, str] = {}

    def add_pool(mid: int, amount: float, user: User | None) -> None:
        if amount <= 0:
            return
        pool_by_master[mid] = money_q2(pool_by_master.get(mid, 0.0) + amount)
        names.setdefault(mid, _master_display_name(user, mid, db))

    def add_correction(mid: int, amount: float, user: User | None = None) -> None:
        if amount <= 0:
            return
        correction_by_master[mid] = money_q2(correction_by_master.get(mid, 0.0) + amount)
        names.setdefault(mid, _master_display_name(user, mid, db))

    def add_help(mid: int, amount: float) -> None:
        if amount <= 0:
            return
        help_by_master[mid] = money_q2(help_by_master.get(mid, 0.0) + amount)
        names.setdefault(mid, _master_display_name(None, mid, db))

    if active_services:
        for vs in active_services:
            pool = float(vs.masters_pool or 0)
            if visit.masters_scope == VisitMastersScope.PER_SERVICE:
                master_rows = vs.masters or []
            else:
                master_rows = visit.masters or []
            for m in master_rows:
                mid = int(m.master_id)
                share = money_q2(pool * float(m.percent or 0) / 100.0)
                add_pool(mid, share, getattr(m, "master", None))
            if getattr(vs, "correction_master_id", None) and float(getattr(vs, "correction_master_amount", 0) or 0) > 0:
                mid = int(vs.correction_master_id)
                add_correction(mid, float(getattr(vs, "correction_master_amount", 0) or 0))
    else:
        pool = float(visit.masters_pool or 0)
        for m in visit.masters or []:
            mid = int(m.master_id)
            share = money_q2(pool * float(m.percent or 0) / 100.0)
            add_pool(mid, share, getattr(m, "master", None))
        if getattr(visit, "correction_master_id", None) and float(getattr(visit, "correction_master_amount", 0) or 0) > 0:
            mid = int(visit.correction_master_id)
            add_correction(mid, float(getattr(visit, "correction_master_amount", 0) or 0))

    for row in hourly_help_rows_from_visit(visit):
        add_help(int(row.master_id), float(row.amount or 0))

    master_ids = sorted(set(pool_by_master) | set(correction_by_master) | set(help_by_master))
    rows: list[VisitMasterPayRow] = []
    for mid in master_ids:
        pool_share = pool_by_master.get(mid, 0.0)
        mix_bonus = 0.0
        correction_bonus = correction_by_master.get(mid, 0.0)
        hourly_help = help_by_master.get(mid, 0.0)
        rows.append(
            VisitMasterPayRow(
                master_id=mid,
                master_name=names.get(mid, _master_display_name(None, mid, db)),
                pool_share=pool_share,
                mix_bonus=mix_bonus,
                correction_bonus=correction_bonus,
                hourly_help=hourly_help,
                total=money_q2(pool_share + mix_bonus + correction_bonus + hourly_help),
            )
        )
    return rows


@dataclass(frozen=True)
class VisitServiceMastersLine:
    service_number: int
    masters_text: str


def _format_master_percent(pct: float) -> str:
    p = float(pct or 0)
    if abs(p - round(p)) < 1e-9:
        return str(int(round(p)))
    s = f"{p:.2f}".rstrip("0").rstrip(".")
    return s


def build_visit_masters_lines(visit: Visit, db: Session | None = None) -> list[VisitServiceMastersLine]:
    """Строки «номер услуги — мастер (доля %)» для карточки визита."""
    active = sorted(
        [vs for vs in (visit.services or []) if not vs.is_cancelled],
        key=lambda s: (int(s.sort_order or 0), int(s.id or 0)),
    )

    def format_master_rows(master_rows: list) -> str:
        parts: list[str] = []
        for m in sorted(master_rows, key=lambda x: (int(x.master_id or 0), int(getattr(x, "id", 0) or 0))):
            name = _master_display_name(getattr(m, "master", None), int(m.master_id), db)
            parts.append(f"{name} ({_format_master_percent(float(m.percent or 0))}%)")
        return ", ".join(parts)

    lines: list[VisitServiceMastersLine] = []
    if active:
        for idx, vs in enumerate(active, start=1):
            if visit.masters_scope == VisitMastersScope.PER_SERVICE:
                master_rows = list(vs.masters or [])
            else:
                master_rows = list(visit.masters or [])
            text = format_master_rows(master_rows)
            if text:
                lines.append(VisitServiceMastersLine(service_number=idx, masters_text=text))
        return lines

    if visit.masters:
        text = format_master_rows(list(visit.masters))
        if text:
            lines.append(VisitServiceMastersLine(service_number=1, masters_text=text))
    return lines
