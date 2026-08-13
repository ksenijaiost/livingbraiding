"""Прайс «Хвосты/резинки»: имя + размер, lookup без жёсткой привязки к опечаткам."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import CatalogProduct

RUBBER_CATEGORY = "Заказ"
RUBBER_SUBCATEGORY = "Хвосты/резинки"

RUBBER_SIZE_ITEMS: tuple[tuple[str, str], ...] = (
    ("MINI", "mini"),
    ("STANDARD", "standard"),
    ("MAX", "max"),
)
RUBBER_SIZE_KEYS = frozenset(k for k, _ in RUBBER_SIZE_ITEMS)
RUBBER_SIZE_LABEL = dict(RUBBER_SIZE_ITEMS)

# Семьи, у которых в форме всегда спрашиваем размер.
RUBBER_SIZED_FAMILIES = frozenset({"TAIL_ELASTIC", "TAIL_CRAB", "TAIL_NET", "TAIL_BUN"})

RUBBER_FAMILY_ITEMS: tuple[tuple[str, str], ...] = (
    ("TAIL_ELASTIC", "Хвост на резинке"),
    ("TAIL_CRAB", "Хвост на крабе"),
    ("TAIL_NET", "Хвост на сетке"),
    ("TAIL_BUN", "Хвост на бублике"),
    ("BRAIDS_ELASTIC", "Косы на резинке"),
)

# Каноническое начало имени в прайсе (без суффикса размера).
RUBBER_FAMILY_BASE_NAME: dict[str, str] = {
    "TAIL_ELASTIC": "Хвост на резинке (1 крепление)",
    "TAIL_CRAB": "Хвост на крабе",
    "TAIL_NET": "Хвост на сетке",
    "TAIL_BUN": "Хвост на бублике",
    "BRAIDS_ELASTIC": "Косы на резинке (1 коса)",
}

_FAMILY_PREFIXES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        ((base, fam) for fam, base in RUBBER_FAMILY_BASE_NAME.items()),
        key=lambda x: len(x[0]),
        reverse=True,
    )
) + (
    ("Хвост на резинке", "TAIL_ELASTIC"),
    ("Косы на резинке", "BRAIDS_ELASTIC"),
)

_SIZE_ALIAS = {
    "mini": "MINI",
    "standard": "STANDARD",
    "standar": "STANDARD",
    "max": "MAX",
}

# « — mini» / « mini» / «-mini» / опечатка standar в конце имени.
_SIZE_SUFFIX_RE = re.compile(
    r"(?:\s*[—–−-]\s*|\s+)(mini|standard|standar|max)\s*$",
    re.IGNORECASE,
)

_FAMILY_PREFIXES_CHECK: tuple[tuple[str, str], ...] = tuple(
    sorted(_FAMILY_PREFIXES, key=lambda x: len(x[0]), reverse=True)
)


def rubber_family_items() -> list[tuple[str, str]]:
    return list(RUBBER_FAMILY_ITEMS)


def rubber_size_items() -> list[tuple[str, str]]:
    return list(RUBBER_SIZE_ITEMS)


def rubber_family_size_from_type(rubber_type: str) -> tuple[str, str]:
    rt = (rubber_type or "").strip()
    if not rt:
        return "", ""
    for prefix in ("TAIL_ELASTIC", "BRAIDS_ELASTIC", "TAIL_CRAB", "TAIL_NET", "TAIL_BUN"):
        if rt == prefix:
            return prefix, ""
        if rt.startswith(prefix + "_"):
            size = rt[len(prefix) + 1 :].strip().upper()
            if size in RUBBER_SIZE_KEYS:
                return prefix, size
            return prefix, ""
    return "", ""


def rubber_type_code(family: str, size: str = "") -> str:
    fam = (family or "").strip()
    sz = (size or "").strip().upper()
    if sz in RUBBER_SIZE_KEYS:
        return f"{fam}_{sz}"
    return fam


def family_needs_size(family: str) -> bool:
    return (family or "").strip() in RUBBER_SIZED_FAMILIES


def rubber_uses_attach_qty(rubber_type: str) -> bool:
    fam, _ = rubber_family_size_from_type(rubber_type)
    return fam == "TAIL_ELASTIC"


def rubber_uses_braids_qty(rubber_type: str) -> bool:
    fam, _ = rubber_family_size_from_type(rubber_type)
    return fam == "BRAIDS_ELASTIC"


def default_is_per_unit(family: str) -> bool:
    return family in ("TAIL_ELASTIC", "BRAIDS_ELASTIC")


def split_rubber_catalog_name(name: str) -> tuple[str, str | None]:
    n = str(name or "").strip()
    if not n:
        return "", None
    m = _SIZE_SUFFIX_RE.search(n)
    if not m:
        return n, None
    size = _SIZE_ALIAS.get(m.group(1).lower())
    base = n[: m.start()].rstrip(" —–−-").strip()
    return base, size


def canonical_rubber_catalog_name(base: str, size: str | None) -> str:
    b = str(base or "").strip()
    if not size:
        return b
    label = RUBBER_SIZE_LABEL.get(size.upper(), size.lower())
    return f"{b} — {label}"


def family_from_base_name(base: str) -> str:
    b = str(base or "").strip()
    if not b:
        return ""
    for prefix, fam in _FAMILY_PREFIXES_CHECK:
        if b == prefix or b.startswith(prefix):
            return fam
    return ""


def rubber_type_from_catalog_name(name: str) -> str:
    base, size = split_rubber_catalog_name(name)
    fam = family_from_base_name(base)
    if not fam:
        return ""
    return rubber_type_code(fam, size or "")


def rubber_service_name(rubber_type: str) -> str:
    fam, size = rubber_family_size_from_type(rubber_type)
    if not fam:
        return str(rubber_type or "").strip()
    base = RUBBER_FAMILY_BASE_NAME.get(fam, fam)
    if family_needs_size(fam) and not size:
        return base
    if size:
        return canonical_rubber_catalog_name(base, size)
    return base


def rubber_type_items() -> list[tuple[str, str]]:
    """Плоский список для брони/калькулятора: семьи с размером — три пункта."""
    out: list[tuple[str, str]] = []
    for fam, label in RUBBER_FAMILY_ITEMS:
        if family_needs_size(fam):
            for size, slabel in RUBBER_SIZE_ITEMS:
                out.append((rubber_type_code(fam, size), f"{label} — {slabel}"))
        else:
            out.append((fam, label))
    return out


def valid_rubber_types() -> frozenset[str]:
    return frozenset(k for k, _ in rubber_type_items()) | frozenset(
        fam for fam, _ in RUBBER_FAMILY_ITEMS
    )


def resolve_rubber_type_for_pricing(rubber_type: str) -> str:
    """Старый TAIL_ELASTIC без размера → STANDARD."""
    fam, size = rubber_family_size_from_type(rubber_type)
    if not fam:
        return (rubber_type or "").strip()
    if family_needs_size(fam) and not size:
        return rubber_type_code(fam, "STANDARD")
    return rubber_type_code(fam, size)


def _parse_meta(raw: str | None) -> dict[str, Any]:
    try:
        meta = json.loads(raw or "{}")
    except Exception:
        meta = {}
    return meta if isinstance(meta, dict) else {}


def list_rubber_catalog_rows(db: Session, *, active_only: bool = False) -> list[CatalogProduct]:
    stmt = select(CatalogProduct).where(
        CatalogProduct.category_name == RUBBER_CATEGORY,
        CatalogProduct.subcategory_name == RUBBER_SUBCATEGORY,
    )
    if active_only:
        stmt = stmt.where(CatalogProduct.is_active.is_(True))
    return list(db.scalars(stmt.order_by(CatalogProduct.sort_order.asc(), CatalogProduct.id.asc())).all())


def _row_matches_family_size(row: CatalogProduct, family: str, size: str) -> bool:
    base, row_size = split_rubber_catalog_name(row.name)
    row_fam = family_from_base_name(base)
    if row_fam != family:
        return False
    want = (size or "").strip().upper()
    got = (row_size or "").strip().upper()
    if want:
        return got == want
    return not got


def find_rubber_catalog_product(db: Session, rubber_type: str) -> CatalogProduct | None:
    lookup = resolve_rubber_type_for_pricing(rubber_type)
    fam, size = rubber_family_size_from_type(lookup)
    if not fam:
        return None
    rows = list_rubber_catalog_rows(db, active_only=True)
    for row in rows:
        if _row_matches_family_size(row, fam, size):
            return row
    if size == "STANDARD":
        for row in rows:
            if _row_matches_family_size(row, fam, ""):
                return row
    return None


def rubber_pricing_tuple(row: CatalogProduct, family: str) -> tuple[float, float, float, bool, str | None]:
    meta = _parse_meta(row.meta_json)
    mp = float(meta.get("master_pay") or 0.0)
    sp = float(meta.get("studio_pay") or 0.0)
    fx = float(meta.get("fixed_expense") or 0.0)
    if meta.get("is_per_unit") is None:
        is_per_unit = default_is_per_unit(family)
    else:
        is_per_unit = bool(meta.get("is_per_unit"))
    unit_label = meta.get("unit_label") or None
    if is_per_unit and not unit_label:
        if family == "TAIL_ELASTIC":
            unit_label = "крепление"
        elif family == "BRAIDS_ELASTIC":
            unit_label = "коса"
    return mp, sp, fx, is_per_unit, (str(unit_label) if unit_label else None)


def rubber_price_meta_by_type(db: Session) -> dict[str, dict[str, float | bool | None]]:
    """Карта для превью формы: ключ — TAIL_ELASTIC_MINI и т.п."""
    out: dict[str, dict[str, float | bool | None]] = {}
    for row in list_rubber_catalog_rows(db, active_only=True):
        rt = rubber_type_from_catalog_name(row.name)
        if not rt:
            continue
        fam, _sz = rubber_family_size_from_type(rt)
        mp, sp, fx, is_per_unit, _ul = rubber_pricing_tuple(row, fam)
        cp = float(row.price) if row.price is not None else None
        payload = {
            "client_price": cp,
            "client_from": cp,
            "client_to": cp,
            "master_pay": mp,
            "studio_pay": sp,
            "fixed_expense": fx,
            "is_per_unit": is_per_unit,
        }
        out[rt] = payload
        if _sz == "STANDARD":
            out.setdefault(fam, payload)
    return out


def rubber_types_for_catalog_name(name: str) -> list[str]:
    rt = rubber_type_from_catalog_name(name)
    if not rt:
        return []
    fam, size = rubber_family_size_from_type(rt)
    out = [rt]
    if family_needs_size(fam) and size == "STANDARD":
        out.append(fam)
    if family_needs_size(fam) and not size:
        out.extend([fam, rubber_type_code(fam, "STANDARD")])
    # уникальные, порядок сохранится
    seen: set[str] = set()
    uniq: list[str] = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def normalize_rubber_catalog_names(db: Session) -> int:
    """Привести суффиксы размера к « — mini|standard|max». Конфликт имён пропускаем."""
    rows = list_rubber_catalog_rows(db, active_only=False)
    existing = {str(r.name or "").strip() for r in rows}
    changed = 0
    for row in rows:
        base, size = split_rubber_catalog_name(row.name)
        if not size:
            continue
        canon = canonical_rubber_catalog_name(base, size)
        meta = _parse_meta(row.meta_json)
        meta_changed = meta.get("size") != size
        if meta_changed:
            meta["size"] = size
        name_changed = row.name != canon
        if name_changed and canon in existing:
            if meta_changed:
                row.meta_json = json.dumps(meta, ensure_ascii=False)
                changed += 1
            continue
        if name_changed:
            existing.discard(row.name)
            row.name = canon
            existing.add(canon)
        if meta_changed:
            row.meta_json = json.dumps(meta, ensure_ascii=False)
        if name_changed or meta_changed:
            changed += 1
    return changed


def _copy_meta_for_size(src_meta: dict[str, Any], *, family: str, size: str) -> dict[str, Any]:
    meta = dict(src_meta)
    meta["size"] = size
    if meta.get("is_per_unit") is None:
        meta["is_per_unit"] = default_is_per_unit(family)
        if family == "TAIL_ELASTIC":
            meta.setdefault("unit_label", "крепление")
        elif family == "BRAIDS_ELASTIC":
            meta.setdefault("unit_label", "коса")
    return meta


def enable_rubber_sizes(db: Session, row: CatalogProduct) -> None:
    """Разбить позицию на mini/standard/max. Цены исходной строки остаются на standard."""
    base, _cur_size = split_rubber_catalog_name(row.name)
    fam = family_from_base_name(base)
    group_rows = [
        r
        for r in list_rubber_catalog_rows(db, active_only=False)
        if split_rubber_catalog_name(r.name)[0] == base
        and r.category_name == row.category_name
        and r.subcategory_name == row.subcategory_name
    ]
    by_size: dict[str, CatalogProduct] = {}
    unsized: CatalogProduct | None = None
    for r in group_rows:
        _b, sz = split_rubber_catalog_name(r.name)
        if sz:
            by_size[sz] = r
        else:
            unsized = r

    if "STANDARD" not in by_size:
        std_src = unsized or row
        std_src.name = canonical_rubber_catalog_name(base, "STANDARD")
        std_src.meta_json = json.dumps(
            _copy_meta_for_size(_parse_meta(std_src.meta_json), family=fam, size="STANDARD"),
            ensure_ascii=False,
        )
        std_src.is_active = True
        by_size["STANDARD"] = std_src

    max_sort = max((int(r.sort_order or 0) for r in group_rows), default=int(row.sort_order or 0))
    for sz, _label in RUBBER_SIZE_ITEMS:
        if sz in by_size:
            r = by_size[sz]
            r.name = canonical_rubber_catalog_name(base, sz)
            r.meta_json = json.dumps(
                _copy_meta_for_size(_parse_meta(r.meta_json), family=fam, size=sz),
                ensure_ascii=False,
            )
            r.is_active = True
            continue
        max_sort += 1
        db.add(
            CatalogProduct(
                category_name=row.category_name,
                subcategory_name=row.subcategory_name,
                name=canonical_rubber_catalog_name(base, sz),
                price=None,
                meta_json=json.dumps(_copy_meta_for_size({}, family=fam, size=sz), ensure_ascii=False),
                sort_order=max_sort,
                is_active=True,
            )
        )

    if unsized is not None:
        _b, sz = split_rubber_catalog_name(unsized.name)
        if not sz:
            unsized.is_active = False


def disable_rubber_sizes(db: Session, row: CatalogProduct) -> None:
    """Свернуть группу в одну строку без размера; цены берём со standard."""
    base, _size = split_rubber_catalog_name(row.name)
    group_rows = [
        r
        for r in list_rubber_catalog_rows(db, active_only=False)
        if split_rubber_catalog_name(r.name)[0] == base
        and r.category_name == row.category_name
        and r.subcategory_name == row.subcategory_name
    ]
    by_size: dict[str, CatalogProduct] = {}
    unsized: CatalogProduct | None = None
    for r in group_rows:
        _b, sz = split_rubber_catalog_name(r.name)
        if sz:
            by_size[sz] = r
        else:
            unsized = r
    keep = by_size.get("STANDARD") or unsized or row
    keep.name = base
    meta = _parse_meta(keep.meta_json)
    meta.pop("size", None)
    keep.meta_json = json.dumps(meta, ensure_ascii=False)
    keep.is_active = True
    for r in group_rows:
        if r.id == keep.id:
            continue
        r.is_active = False


def rename_rubber_group(db: Session, row: CatalogProduct, new_base: str) -> None:
    new_base = str(new_base or "").strip()
    if not new_base:
        return
    old_base, _ = split_rubber_catalog_name(row.name)
    if old_base == new_base:
        return
    for r in list_rubber_catalog_rows(db, active_only=False):
        b, sz = split_rubber_catalog_name(r.name)
        if b != old_base:
            continue
        r.name = canonical_rubber_catalog_name(new_base, sz)


def ensure_rubber_size_row(db: Session, *, base_name: str, size: str, template: CatalogProduct) -> CatalogProduct:
    size = size.strip().upper()
    name = canonical_rubber_catalog_name(base_name, size)
    existing = db.scalar(
        select(CatalogProduct).where(
            CatalogProduct.category_name == template.category_name,
            CatalogProduct.subcategory_name == template.subcategory_name,
            CatalogProduct.name == name,
        )
    )
    if existing:
        existing.is_active = True
        return existing
    fam = family_from_base_name(base_name)
    max_sort = db.scalar(
        select(func.max(CatalogProduct.sort_order)).where(
            CatalogProduct.category_name == template.category_name,
            CatalogProduct.subcategory_name == template.subcategory_name,
        )
    )
    row = CatalogProduct(
        category_name=template.category_name,
        subcategory_name=template.subcategory_name,
        name=name,
        price=None,
        meta_json=json.dumps(_copy_meta_for_size({}, family=fam, size=size), ensure_ascii=False),
        sort_order=int(max_sort or 0) + 1,
        is_active=True,
    )
    db.add(row)
    return row


def build_rubber_catalog_display(rows: list[Any]) -> list[SimpleNamespace]:
    """Группы для таблицы прайса: шапка + 3 размера или обычная строка."""
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for r in rows:
        base, size = split_rubber_catalog_name(r.name)
        key = base or str(r.name or "")
        if key not in groups:
            groups[key] = {"unsized": None, "sizes": {}, "sort": (0 if r.is_active else 1, str(r.name or ""))}
            order.append(key)
        g = groups[key]
        if size:
            g["sizes"][size] = r
        else:
            g["unsized"] = r

    out: list[SimpleNamespace] = []
    for base in order:
        g = groups[base]
        sizes: dict[str, Any] = g["sizes"]
        unsized = g["unsized"]
        has_sizes = bool(sizes)
        if has_sizes:
            group_row = sizes.get("STANDARD") or next(iter(sizes.values()))
            out.append(
                SimpleNamespace(
                    row_kind="group_parent",
                    id=int(group_row.id) if group_row.id else None,
                    name=base,
                    base_name=base,
                    size_key="",
                    size_label="",
                    size_checked=True,
                    price_locked=True,
                    indent=False,
                    missing=False,
                    is_active=any(bool(getattr(x, "is_active", False)) for x in sizes.values()),
                    price=None,
                    master_pay=None,
                    fixed_expense=None,
                    kit_key=None,
                    ignore_in_calc=False,
                    is_used_in_kit_form=False,
                    is_bu=False,
                    category_name=getattr(group_row, "category_name", RUBBER_CATEGORY),
                    subcategory_name=getattr(group_row, "subcategory_name", RUBBER_SUBCATEGORY),
                )
            )
            for sz, slabel in RUBBER_SIZE_ITEMS:
                child = sizes.get(sz)
                if child is not None:
                    out.append(
                        SimpleNamespace(
                            row_kind="size_child",
                            id=child.id,
                            name=child.name,
                            base_name=base,
                            size_key=sz,
                            size_label=slabel,
                            size_checked=True,
                            price_locked=False,
                            indent=True,
                            missing=False,
                            is_active=child.is_active,
                            price=child.price,
                            master_pay=child.master_pay,
                            fixed_expense=child.fixed_expense,
                            kit_key=getattr(child, "kit_key", None),
                            ignore_in_calc=getattr(child, "ignore_in_calc", False),
                            is_used_in_kit_form=getattr(child, "is_used_in_kit_form", False),
                            is_bu=getattr(child, "is_bu", False),
                            category_name=child.category_name,
                            subcategory_name=child.subcategory_name,
                        )
                    )
                else:
                    out.append(
                        SimpleNamespace(
                            row_kind="size_placeholder",
                            id=int(group_row.id) if group_row.id else None,
                            name=canonical_rubber_catalog_name(base, sz),
                            base_name=base,
                            size_key=sz,
                            size_label=slabel,
                            size_checked=True,
                            price_locked=True,
                            indent=True,
                            missing=True,
                            is_active=False,
                            price=None,
                            master_pay=None,
                            fixed_expense=None,
                            kit_key=None,
                            ignore_in_calc=False,
                            is_used_in_kit_form=False,
                            is_bu=False,
                            category_name=getattr(group_row, "category_name", RUBBER_CATEGORY),
                            subcategory_name=getattr(group_row, "subcategory_name", RUBBER_SUBCATEGORY),
                        )
                    )
            continue
        if unsized is None:
            continue
        out.append(
            SimpleNamespace(
                row_kind="plain",
                id=unsized.id,
                name=unsized.name,
                base_name=base,
                size_key="",
                size_label="",
                size_checked=False,
                price_locked=False,
                indent=False,
                missing=False,
                is_active=unsized.is_active,
                price=unsized.price,
                master_pay=unsized.master_pay,
                fixed_expense=unsized.fixed_expense,
                kit_key=getattr(unsized, "kit_key", None),
                ignore_in_calc=getattr(unsized, "ignore_in_calc", False),
                is_used_in_kit_form=getattr(unsized, "is_used_in_kit_form", False),
                is_bu=getattr(unsized, "is_bu", False),
                category_name=unsized.category_name,
                subcategory_name=unsized.subcategory_name,
            )
        )
    return out
