"""
Access-лог HTTP: время, клиент, запрос, статус, длительность, логин по сессии.

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
_SKIP_USER_DB_PREFIXES: tuple[str, ...] = ("/static/", "/media/")


def configure_request_access_logging() -> None:
    """Один раз при старте: форматированный access-лог и отключение uvicorn.access."""
    log = logging.getLogger(ACCESS_LOGGER_NAME)
    log.handlers.clear()
    log.setLevel(logging.INFO)
    log.propagate = False

    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    log.addHandler(handler)

    logging.getLogger("uvicorn.access").disabled = True


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
            logging.getLogger(ACCESS_LOGGER_NAME).info(line)
