"""HTTP к Max API: certifi плюс сертификаты Минцифры из MAX_CA_BUNDLE.

Проверку SSL не отключаем. VK, Telegram и остальные хосты этим модулем не ходят.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import certifi
import httpx

from app.settings import get_settings

logger = logging.getLogger(__name__)

_cached_bundle: str | None = None


def clear_max_ca_bundle_cache() -> None:
    global _cached_bundle
    _cached_bundle = None


def max_ca_bundle_path() -> str:
    """Путь к PEM для verify. Собирается один раз и кэшируется."""
    global _cached_bundle
    if _cached_bundle is not None:
        return _cached_bundle
    _cached_bundle = _build_max_ca_bundle()
    return _cached_bundle


def _build_max_ca_bundle() -> str:
    certifi_path = certifi.where()
    extra = (get_settings().max_ca_bundle or "").strip()
    if not extra:
        logger.warning(
            "MAX_CA_BUNDLE не задан: запросы к Max идут с обычным certifi. "
            "Для platform-api2.max.ru нужен PEM Минцифры (корневой и промежуточный)."
        )
        return certifi_path
    extra_path = Path(extra)
    if not extra_path.is_file():
        logger.warning(
            "MAX_CA_BUNDLE=%s не найден: запросы к Max идут с обычным certifi.",
            extra,
        )
        return certifi_path
    base = Path(certifi_path).read_bytes()
    addon = extra_path.read_bytes()
    if base and not base.endswith(b"\n"):
        base += b"\n"
    blob = base + addon
    if blob and not blob.endswith(b"\n"):
        blob += b"\n"
    fd, name = tempfile.mkstemp(prefix="lb-max-ca-", suffix=".pem")
    try:
        os.write(fd, blob)
    finally:
        os.close(fd)
    logger.info("Собран бандл CA для Max: certifi + %s", extra)
    return name


def max_request(
    method: str,
    url: str,
    *,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 8,
) -> tuple[int, bytes]:
    """Запрос к Max. verify — путь к бандлу, никогда не False."""
    bundle = max_ca_bundle_path()
    with httpx.Client(verify=bundle, timeout=timeout) as client:
        response = client.request(method, url, content=content, headers=headers)
    return response.status_code, response.content
