from __future__ import annotations

"""Дружелюбный ответ при необработанных 500 вместо голого Internal Server Error."""

import logging
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.requests import ClientDisconnect
from starlette.responses import HTMLResponse, Response

logger = logging.getLogger("livingbraiding.app")

TECH_ERROR_QUERY = "tech_err"
CLIENT_DISCONNECT_STATUS = 499

TECH_ERROR_USER_MESSAGE = (
    "Произошла техническая ошибка. Напишите техническому специалисту и опишите все ваши действия. "
    "Можно повторить один раз, записав видео с экрана. "
    "Не нажимайте «сохранить» много раз подряд — это засоряет логи и не помогает."
)

TECH_ERROR_JSON = {
    "ok": False,
    "error": "technical",
    "message": TECH_ERROR_USER_MESSAGE,
}

_TECH_ERROR_FALLBACK_HTML = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Техническая ошибка</title></head>
<body style="font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;">
<h2>Техническая ошибка</h2>
<p>{TECH_ERROR_USER_MESSAGE}</p>
<p><a href="javascript:history.back()">Вернуться назад</a> · <a href="/">На главную</a></p>
</body></html>"""


def is_client_disconnect(exc: BaseException | None, _seen: set[int] | None = None) -> bool:
    """Клиент закрыл соединение (вкладка, таймаут, обрыв сети) — не баг сервера."""
    if exc is None:
        return False
    seen = _seen if _seen is not None else set()
    exc_id = id(exc)
    if exc_id in seen:
        return False
    seen.add(exc_id)
    if isinstance(exc, ClientDisconnect):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(is_client_disconnect(e, seen) for e in exc.exceptions)
    if is_client_disconnect(exc.__cause__, seen) or is_client_disconnect(exc.__context__, seen):
        return True
    return False


def client_disconnect_response() -> Response:
    """Nginx-style 499: клиент оборвал запрос, ответ уже никому не нужен."""
    return Response(status_code=CLIENT_DISCONNECT_STATUS)


def wants_json_error(request: Request) -> bool:
    path = request.url.path or ""
    if path.startswith("/api/"):
        return True
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept and "text/html" not in accept:
        return True
    return False


def recovery_get_url(request: Request) -> str:
    """GET на тот же путь с меткой баннера (после сбоя POST формы)."""
    pairs = [(k, v) for k, v in request.query_params.multi_items() if k != TECH_ERROR_QUERY]
    pairs.append((TECH_ERROR_QUERY, "1"))
    qs = urlencode(pairs)
    path = request.url.path or "/"
    return f"{path}?{qs}" if qs else path


def request_shows_tech_error_banner(request: Request) -> bool:
    return (request.query_params.get(TECH_ERROR_QUERY) or "").strip() in ("1", "true", "yes")


async def client_disconnect_handler(request: Request, exc: Exception) -> Response:
    logger.info(
        "Client disconnected %s %s",
        request.method,
        request.url.path,
    )
    return client_disconnect_response()


async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    if is_client_disconnect(exc):
        return await client_disconnect_handler(request, exc)

    # HTTPException и RequestValidationError обрабатываются своими хендлерами (более узкий тип).
    logger.exception(
        "Unhandled error %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
    )

    if wants_json_error(request):
        return JSONResponse(TECH_ERROR_JSON, status_code=500)

    method = (request.method or "GET").upper()
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        # Вернуть на ту же страницу формы (GET) с баннером — не пугать голым 500.
        return RedirectResponse(url=recovery_get_url(request), status_code=303)

    from app.webui import ctx, templates

    try:
        return templates.TemplateResponse(
            "tech_error.html",
            ctx(request, current_user=None, message=TECH_ERROR_USER_MESSAGE),
            status_code=500,
        )
    except Exception:
        logger.exception("Failed to render tech_error.html for %s %s", request.method, request.url.path)
        return HTMLResponse(_TECH_ERROR_FALLBACK_HTML, status_code=500)


def register_tech_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ClientDisconnect, client_disconnect_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


class SwallowClientDisconnectMiddleware:
    """Обрыв клиента на send/parse не должен всплывать в uvicorn как 500."""

    def __init__(self, app):  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        except BaseException as exc:
            if is_client_disconnect(exc):
                logger.info(
                    "Client disconnected %s %s",
                    scope.get("method", "?"),
                    scope.get("path", "?"),
                )
                return
            raise
