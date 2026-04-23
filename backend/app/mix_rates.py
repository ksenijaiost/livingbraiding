"""Коэффициенты смешки (₽/г) из work_rates с дефолтами и поддержкой старых ключей."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import MixComplexity, WorkRate
from app.work_rate_keys import (
    MIX_HARD_LEGACY,
    MIX_KANEK,
    MIX_LENGTH,
    MIX_LIGHT,
    MIX_MEDIUM_LEGACY,
    MIX_SIMPLE_LEGACY,
    MIX_STANDARD,
    MIX_THERMO,
)


def read_work_rate_float(db: Session, key: str) -> float | None:
    r = db.scalar(select(WorkRate).where(WorkRate.key == key, WorkRate.is_active.is_(True)))
    if not r:
        return None
    try:
        return float(json.loads(r.value_json))
    except Exception:
        return None


def mix_complexity_rate_map(db: Session) -> dict[MixComplexity, float]:
    """Актуальные ₽/г по сложности. Новые ключи: mix_light … mix_length; легаси: mix_simple/medium/hard."""

    def one(primary: str, default: float, *legacy: str) -> float:
        v = read_work_rate_float(db, primary)
        if v is not None:
            return max(0.0, float(v))
        for lk in legacy:
            v = read_work_rate_float(db, lk)
            if v is not None:
                return max(0.0, float(v))
        return default

    return {
        MixComplexity.LIGHT: one(MIX_LIGHT, 0.5),
        MixComplexity.STANDARD: one(MIX_STANDARD, 1.0, MIX_SIMPLE_LEGACY),
        MixComplexity.KANEK: one(MIX_KANEK, 1.5, MIX_MEDIUM_LEGACY),
        MixComplexity.THERMO: one(MIX_THERMO, 2.0, MIX_HARD_LEGACY),
        MixComplexity.LENGTH: one(MIX_LENGTH, 2.5),
    }


def mix_complexity_rate_for(db: Session, mc: MixComplexity | None) -> float:
    if mc is None:
        return 0.0
    return float(mix_complexity_rate_map(db).get(mc, 0.0))


def mix_rates_for_admin_form(db: Session) -> dict[str, float]:
    """Пять полей для шаблона настроек (уже с подмесом легаси-ключей)."""
    m = mix_complexity_rate_map(db)
    return {
        MIX_LIGHT: m[MixComplexity.LIGHT],
        MIX_STANDARD: m[MixComplexity.STANDARD],
        MIX_KANEK: m[MixComplexity.KANEK],
        MIX_THERMO: m[MixComplexity.THERMO],
        MIX_LENGTH: m[MixComplexity.LENGTH],
    }


def mix_rates_meta_json_dict(db: Session) -> dict[str, float]:
    """Для JSON в формах (ключ = значение enum MixComplexity)."""
    m = mix_complexity_rate_map(db)
    return {k.value: float(v) for k, v in m.items()}
