"""Коэффициенты смешки (₽/г) из work_rates с дефолтами и поддержкой старых ключей."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import MixComplexity, WorkRate


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
        MixComplexity.LIGHT: one("mix_light", 0.5),
        MixComplexity.STANDARD: one("mix_standard", 1.0, "mix_simple"),
        MixComplexity.KANEK: one("mix_kanek", 1.5, "mix_medium"),
        MixComplexity.THERMO: one("mix_thermo", 2.0, "mix_hard"),
        MixComplexity.LENGTH: one("mix_length", 2.5),
    }


def mix_complexity_rate_for(db: Session, mc: MixComplexity | None) -> float:
    if mc is None:
        return 0.0
    return float(mix_complexity_rate_map(db).get(mc, 0.0))


def mix_rates_for_admin_form(db: Session) -> dict[str, float]:
    """Пять полей для шаблона настроек (уже с подмесом легаси-ключей)."""
    m = mix_complexity_rate_map(db)
    return {
        "mix_light": m[MixComplexity.LIGHT],
        "mix_standard": m[MixComplexity.STANDARD],
        "mix_kanek": m[MixComplexity.KANEK],
        "mix_thermo": m[MixComplexity.THERMO],
        "mix_length": m[MixComplexity.LENGTH],
    }


def mix_rates_meta_json_dict(db: Session) -> dict[str, float]:
    """Для JSON в формах (ключ = значение enum MixComplexity)."""
    m = mix_complexity_rate_map(db)
    return {k.value: float(v) for k, v in m.items()}
