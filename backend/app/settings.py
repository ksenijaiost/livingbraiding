from __future__ import annotations

"""Environment/config loader (dotenv-friendly)."""

import functools
import os

from dotenv import load_dotenv


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


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
        self.telegram_webhook_secret = (os.getenv("TELEGRAM_WEBHOOK_SECRET") or "").strip()
        self.telegram_bot_username = (os.getenv("TELEGRAM_BOT_USERNAME") or "").strip().lstrip("@")
        self.telegram_api_base = (
            (os.getenv("TELEGRAM_API_BASE") or "https://api.telegram.org").strip().rstrip("/")
            or "https://api.telegram.org"
        )
        self.vk_group_token = (os.getenv("VK_GROUP_TOKEN") or "").strip()
        self.vk_group_id = (os.getenv("VK_GROUP_ID") or "").strip()
        self.vk_confirmation_code = (os.getenv("VK_CONFIRMATION_CODE") or "").strip()
        self.vk_secret_key = (os.getenv("VK_SECRET_KEY") or "").strip()
        self.vk_api_version = (os.getenv("VK_API_VERSION") or "5.199").strip() or "5.199"
        # Короткое имя для https://vk.me/<domain>?ref=… (club123 или screen name).
        self.vk_group_domain = (os.getenv("VK_GROUP_DOMAIN") or "").strip().lstrip("@").strip("/")
        # Мессенджер Max (platform-api2.max.ru).
        self.max_bot_token = (os.getenv("MAX_BOT_TOKEN") or "").strip()
        self.max_bot_username = (os.getenv("MAX_BOT_USERNAME") or "").strip().lstrip("@")
        self.max_webhook_secret = (os.getenv("MAX_WEBHOOK_SECRET") or "").strip()
        self.max_api_base = (
            (os.getenv("MAX_API_BASE") or "https://platform-api2.max.ru").strip().rstrip("/")
            or "https://platform-api2.max.ru"
        )
        # Фоновый воркер outbox: на проде по умолчанию вкл., локально/в тестах — выкл.
        self.notification_worker_enabled = _env_bool(
            "NOTIFICATION_WORKER_ENABLED",
            default=(self.app_env == "prod"),
        )
        try:
            interval = int(os.getenv("NOTIFICATION_WORKER_INTERVAL_SECONDS") or "45")
        except ValueError:
            interval = 45
        self.notification_worker_interval_seconds = max(5, min(interval, 3600))


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
