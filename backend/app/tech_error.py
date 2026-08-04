from __future__ import annotations

"""Дружелюбный ответ при необработанных 500 вместо голого Internal Server Error."""

import logging
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.responses import Response

logger = logging.getLogger("livingbraiding.app")

TECH_ERROR_QUERY = "tech_err"

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


async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
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

    return templates.TemplateResponse(
        "tech_error.html",
        ctx(request, current_user=None, message=TECH_ERROR_USER_MESSAGE),
        status_code=500,
    )


def register_tech_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(Exception, unhandled_exception_handler)
