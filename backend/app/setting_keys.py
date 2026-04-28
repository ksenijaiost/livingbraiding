"""Строковые ключи таблицы `settings`.

Цель: единая точка правды для ключей, чтобы избегать опечаток и упростить рефакторинг.
"""

from __future__ import annotations

# Studio / system settings
SALON_CUT_PCT = "salon_cut_pct"
EDIT_WINDOW_DAYS = "edit_window_days"
AUDIT_RETENTION_MONTHS = "audit_retention_months"
DISPLAY_TIMEZONE = "display_timezone"
AUDIT_RETENTION_LAST_RUN_UTC = "audit_retention_last_run_utc"

# Kits
KIT_MAX_RESERVES_PER_KIT = "kit_max_reserves_per_kit"

