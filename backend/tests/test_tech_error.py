from __future__ import annotations

from fastapi import FastAPI
from starlette.requests import Request
from starlette.testclient import TestClient

from app.tech_error import (
    TECH_ERROR_USER_MESSAGE,
    recovery_get_url,
    register_tech_error_handlers,
    wants_json_error,
)


def _request(method: str, path: str, *, accept: str = "text/html", query: str = "") -> Request:
    headers = [(b"accept", accept.encode())]
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query.encode(),
        "headers": headers,
    }
    return Request(scope)


def test_recovery_get_url_adds_flag() -> None:
    req = _request("POST", "/clients/new")
    assert recovery_get_url(req) == "/clients/new?tech_err=1"


def test_recovery_get_url_keeps_other_params() -> None:
    req = _request("POST", "/clients/new", query="foo=1&tech_err=old")
    assert recovery_get_url(req) == "/clients/new?foo=1&tech_err=1"


def test_wants_json_for_api_path() -> None:
    assert wants_json_error(_request("POST", "/api/master-schedule/x")) is True
    assert wants_json_error(_request("POST", "/clients/new")) is False


def test_post_unhandled_redirects_to_form_with_banner() -> None:
    app = FastAPI()
    register_tech_error_handlers(app)

    @app.post("/clients/new")
    def boom() -> None:
        raise RuntimeError("db boom")

    @app.get("/clients/new")
    def form() -> dict:
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    res = client.post("/clients/new", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/clients/new?tech_err=1"


def test_api_unhandled_returns_json() -> None:
    app = FastAPI()
    register_tech_error_handlers(app)

    @app.post("/api/thing")
    def boom() -> None:
        raise RuntimeError("api boom")

    client = TestClient(app, raise_server_exceptions=False)
    res = client.post("/api/thing")
    assert res.status_code == 500
    data = res.json()
    assert data["error"] == "technical"
    assert "техническ" in data["message"].lower()
    assert TECH_ERROR_USER_MESSAGE in data["message"] or "видео" in data["message"]
