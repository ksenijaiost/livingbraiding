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


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

