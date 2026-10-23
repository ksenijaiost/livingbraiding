"""HTTP к Max API: certifi плюс сертификаты Минцифры (MAX_CA_PEM или файл MAX_CA_BUNDLE).

Проверку SSL не отключаем. VK, Telegram и остальные хосты этим модулем не ходят.
"""

from __future__ import annotations

import base64
import binascii
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


def _decode_max_ca_pem(raw: str) -> bytes | None:
    """PEM из переменной: как есть, с литералами \\n или в base64."""
    text = raw.strip().lstrip("\ufeff")
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if not text:
        return None
    if text.startswith("-----BEGIN"):
        return text.replace("\\n", "\n").encode("utf-8")
    compact = "".join(text.split())
    pad = (-len(compact)) % 4
    try:
        decoded = base64.b64decode(compact + ("=" * pad), validate=True)
    except (binascii.Error, ValueError):
        return None
    if not decoded.lstrip().startswith(b"-----BEGIN"):
        return None
    return decoded


def _write_ca_bundle(addon: bytes) -> str:
    base = Path(certifi.where()).read_bytes()
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
    return name


def _build_max_ca_bundle() -> str:
    certifi_path = certifi.where()
    settings = get_settings()
    pem = _decode_max_ca_pem(settings.max_ca_pem or "")
    if pem is not None:
        path = _write_ca_bundle(pem)
        logger.info("Собран бандл CA для Max: certifi + MAX_CA_PEM")
        return path
    if (settings.max_ca_pem or "").strip():
        logger.warning(
            "MAX_CA_PEM задан, но это не PEM и не base64 с сертификатом. "
            "Дальше проверяется файл MAX_CA_BUNDLE."
        )
    extra = (settings.max_ca_bundle or "").strip()
    if not extra:
        logger.warning(
            "MAX_CA_PEM и MAX_CA_BUNDLE не заданы: запросы к Max идут с обычным certifi. "
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
    path = _write_ca_bundle(extra_path.read_bytes())
    logger.info("Собран бандл CA для Max: certifi + %s", extra)
    return path


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
    max_request.last_headers = {str(key).lower(): value for key, value in response.headers.items()}
    return response.status_code, response.content
