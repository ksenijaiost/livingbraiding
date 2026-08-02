"""
Access-лог HTTP: уровень, время, клиент, запрос, статус, длительность, логин по сессии.

Стандартный `uvicorn.access` отключается, чтобы не дублировать строки без времени.
"""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.auth import optional_session_user_id
from app.db.models import User
from app.db.session import SessionLocal

ACCESS_LOGGER_NAME = "livingbraiding.access"
APP_LOGGER_NAME = "livingbraiding.app"
_SKIP_USER_DB_PREFIXES: tuple[str, ...] = ("/static/", "/media/")


def _stream_handler_with_level() -> logging.StreamHandler:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    return handler


def configure_request_access_logging() -> None:
    """Один раз при старте: форматированный access-лог и отключение uvicorn.access."""
    log = logging.getLogger(ACCESS_LOGGER_NAME)
    log.handlers.clear()
    log.setLevel(logging.INFO)
    log.propagate = False
    log.addHandler(_stream_handler_with_level())

    app_log = logging.getLogger(APP_LOGGER_NAME)
    if not any(isinstance(h, logging.StreamHandler) for h in app_log.handlers):
        app_log.addHandler(_stream_handler_with_level())
        app_log.setLevel(logging.INFO)
        app_log.propagate = False

    logging.getLogger("uvicorn.access").disabled = True


def access_log_level_for_status(status: int, *, has_validation_error: bool) -> int:
    """
    INFO — успех и редиректы;
    WARNING — пользовательская валидация (как logger.warning в form_validation_log);
    ERROR — прочие 4xx/5xx.
    """
    if 200 <= status < 400:
        return logging.INFO
    if has_validation_error and 400 <= status < 500:
        return logging.WARNING
    return logging.ERROR


def _user_label_for_request(request: Request) -> str:
    path = request.url.path
    if any(path.startswith(p) for p in _SKIP_USER_DB_PREFIXES):
        return "-"
    uid = optional_session_user_id(request)
    if not uid:
        return "-"
    db = SessionLocal()
    try:
        u = db.get(User, uid)
        if not u or not u.is_active:
            return "-"
        name = (u.username or "").strip()
        return name or str(u.id)
    finally:
        db.close()


class AccessLogWithUserMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        start = time.perf_counter()
        response: Response | None = None
        try:
            response = await call_next(request)
            return response
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            status = int(response.status_code) if response is not None else 500
            client_host = request.client.host if request.client else "-"
            user_label = _user_label_for_request(request)
            path_qs = request.url.path
            if request.url.query:
                path_qs = f"{path_qs}?{request.url.query}"
            line = (
                f'{client_host} | {request.method} "{path_qs} HTTP/1.1" '
                f"| {status} | {elapsed_ms:.0f} ms | user={user_label}"
            )
            validation_err = getattr(request.state, "validation_error", None)
            if validation_err:
                line = f"{line} | err={validation_err}"
            level = access_log_level_for_status(status, has_validation_error=bool(validation_err))
            logging.getLogger(ACCESS_LOGGER_NAME).log(level, line)
