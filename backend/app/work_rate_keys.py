"""Строковые ключи таблицы `work_rates` (value_json: JSON-скаляр).

Ключи используются в настройках/расчётах; вынесены, чтобы уменьшить риск опечаток.
"""

from __future__ import annotations

# Studio share (доля "студии" в work economics; 0..1)
STUDIO_SHARE = "studio_share"
STUDIO_SHARE_OVERRIDE = "studio_share_override"

# Custom order / заказ: множитель к выплате (например резинки/коррекция в заказе)
CUSTOM_ORDER_BONUS_MULTIPLIER = "custom_order_bonus_multiplier"

# Смешка: текущие ключи
MIX_LIGHT = "mix_light"
MIX_STANDARD = "mix_standard"
MIX_KANEK = "mix_kanek"
MIX_THERMO = "mix_thermo"
MIX_LENGTH = "mix_length"

# Легаси-ключи (миграция со старой схемы; читаем как fallback)
MIX_SIMPLE_LEGACY = "mix_simple"
MIX_MEDIUM_LEGACY = "mix_medium"
MIX_HARD_LEGACY = "mix_hard"
