"""3.31: бандл CA для Max — certifi плюс PEM Минцифры, verify не отключается."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import certifi

from app.max_http import clear_max_ca_bundle_cache, max_ca_bundle_path, max_request
from app.settings import get_settings


def _reset(monkeypatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("MAX_CA_BUNDLE", raising=False)
        monkeypatch.setenv("MAX_CA_BUNDLE", "")
    else:
        monkeypatch.setenv("MAX_CA_BUNDLE", value)
    get_settings.cache_clear()
    clear_max_ca_bundle_cache()


def test_bundle_appends_max_ca_file(monkeypatch, tmp_path) -> None:
    marker = "-----BEGIN CERTIFICATE-----\nLB-MAX-CA-MARKER\n-----END CERTIFICATE-----\n"
    pem = tmp_path / "russian.pem"
    pem.write_text(marker, encoding="utf-8")
    _reset(monkeypatch, str(pem))
    path = max_ca_bundle_path()
    text = Path(path).read_text(encoding="utf-8")
    certifi_text = Path(certifi.where()).read_text(encoding="utf-8")
    assert certifi_text.strip() in text
    assert "LB-MAX-CA-MARKER" in text
    assert path == max_ca_bundle_path()
    assert path != certifi.where()


def test_missing_bundle_uses_certifi_and_warns(monkeypatch, caplog) -> None:
    _reset(monkeypatch, None)
    with caplog.at_level(logging.WARNING, logger="app.max_http"):
        path = max_ca_bundle_path()
    assert path == certifi.where()
    assert "MAX_CA_BUNDLE" in caplog.text
    assert "certifi" in caplog.text


def test_missing_file_uses_certifi_and_warns(monkeypatch, tmp_path, caplog) -> None:
    missing = tmp_path / "no-such.pem"
    _reset(monkeypatch, str(missing))
    with caplog.at_level(logging.WARNING, logger="app.max_http"):
        path = max_ca_bundle_path()
    assert path == certifi.where()
    assert "не найден" in caplog.text


def test_max_client_passes_bundle_verify(monkeypatch, tmp_path) -> None:
    pem = tmp_path / "russian.pem"
    pem.write_text("-----BEGIN CERTIFICATE-----\nQQ==\n-----END CERTIFICATE-----\n", encoding="utf-8")
    _reset(monkeypatch, str(pem))
    bundle = max_ca_bundle_path()
    seen: dict[str, object] = {}

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            seen["verify"] = kwargs.get("verify")

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def request(self, method: str, url: str, content: bytes | None = None, headers: dict | None = None):
            resp = MagicMock()
            resp.status_code = 200
            resp.content = b"{}"
            return resp

    with patch("app.max_http.httpx.Client", _Client):
        status, body = max_request("GET", "https://platform-api2.max.ru/subscriptions")
    assert status == 200
    assert body == b"{}"
    assert seen["verify"] == bundle
    assert seen["verify"] is not False


def test_subscribe_set_posts_webhook(monkeypatch, capsys) -> None:
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sec")
    monkeypatch.setenv("MAX_API_BASE", "https://platform-api2.max.ru")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    get_settings.cache_clear()
    calls: list[tuple[str, str, bytes | None]] = []

    def fake_request(method, url, *, content=None, headers=None, timeout=8):
        calls.append((method, url, content))
        if method == "GET":
            return 200, b'{"subscriptions":[]}'
        return 200, b'{"success":true}'

    monkeypatch.setattr("app.scripts.max_subscribe.max_request", fake_request)
    from app.scripts.max_subscribe import main

    code = main(["--set", "https://crm.example.com/"])
    assert code == 0
    assert calls[0][0] == "GET"
    assert calls[0][1] == "https://platform-api2.max.ru/subscriptions"
    assert calls[1][0] == "POST"
    body = json.loads(calls[1][2] or b"{}")
    assert body["url"] == "https://crm.example.com/webhooks/max"
    assert body["update_types"] == ["message_created", "bot_started"]
    assert body["secret"] == "sec"
    out = capsys.readouterr().out
    assert "Текущие подписки" in out
