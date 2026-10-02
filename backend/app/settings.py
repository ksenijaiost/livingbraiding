from __future__ import annotations

"""Environment/config loader (dotenv-friendly)."""

import functools
import os

from dotenv import load_dotenv


class Settings:
    def __init__(self) -> None:
        load_dotenv()
        self.app_env = os.getenv("APP_ENV", "dev")
        self.secret_key = os.getenv("SECRET_KEY", "change-me")
        # Один файл SQLite = все таблицы внутри. Папка `data/` — чтобы не лежало в корне backend.
        self.database_url = os.getenv("DATABASE_URL", "sqlite:///./data/livingbraiding.db")
        # Разовый backfill: сдвигать effective_at проводок даже в закрытые периоды ЗП.
        raw = (os.getenv("PAYROLL_LEDGER_BACKFILL_CLOSED") or "false").strip().lower()
        self.payroll_ledger_backfill_closed = raw in ("1", "true", "yes", "on")
        self.telegram_bot_token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        self.vk_group_token = (os.getenv("VK_GROUP_TOKEN") or "").strip()


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

