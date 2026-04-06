"""
Человекочитаемые подписи для админки (без имён Enum в интерфейсе).
"""

from __future__ import annotations

import json
from typing import Any

from app.db.models import MixComplexity, MixSource, Visit, VisitClientType, VisitPriceType, VisitService


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
    return {
        "SIMPLE": "Простая",
        "MEDIUM": "Средняя",
        "HARD": "Сложная",
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
            fs = kit.get("from_stock") or {}
            sku = fs.get("sku", "—")
            ent = fs.get("use_entire_kit")
            bu = fs.get("blanks_used", 0)
            if ent:
                blocks.append(("Со склада", f"арт. {sku}, весь комплект"))
            else:
                blocks.append(("Со склада", f"арт. {sku}, заготовок: {_format_card_scalar(bu)}"))
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
