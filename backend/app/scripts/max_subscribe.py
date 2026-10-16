"""Подписки вебхука Max. Запуск: python -m app.scripts.max_subscribe [--set] [URL]."""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

from app.max_http import max_request
from app.settings import get_settings


def _subscriptions_url(api_base: str) -> str:
    return f"{api_base.rstrip('/')}/subscriptions"


def _webhook_url(public_base: str) -> str:
    root = public_base.strip().rstrip("/")
    if root.endswith("/webhooks/max"):
        return root
    return f"{root}/webhooks/max"


def _print_body(status: int, raw: bytes) -> None:
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        print(text)
        return
    print(json.dumps(parsed, ensure_ascii=False, indent=2))
    if status >= 400:
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Подписки вебхука Max (с бандлом CA из MAX_CA_BUNDLE).",
    )
    parser.add_argument(
        "--set",
        action="store_true",
        help="Подписать вебхук на <URL>/webhooks/max",
    )
    parser.add_argument(
        "base_url",
        nargs="?",
        help="Публичный URL сайта. Если не указан, берётся PUBLIC_BASE_URL.",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    token = (settings.max_bot_token or "").strip()
    if not token:
        print("Задайте MAX_BOT_TOKEN.", file=sys.stderr)
        return 1
    api = (settings.max_api_base or "https://platform-api2.max.ru").rstrip("/")
    headers = {"Authorization": token}
    try:
        status, raw = max_request("GET", _subscriptions_url(api), headers=headers)
    except httpx.HTTPError as exc:
        print(f"Max сеть: {exc}", file=sys.stderr)
        return 1
    print("Текущие подписки:")
    try:
        _print_body(status, raw)
    except SystemExit:
        return 1
    if not args.set:
        return 0
    public = (args.base_url or os.getenv("PUBLIC_BASE_URL") or "").strip()
    if not public:
        print("Укажите URL аргументом или PUBLIC_BASE_URL.", file=sys.stderr)
        return 1
    secret = (settings.max_webhook_secret or "").strip()
    if not secret:
        print("Задайте MAX_WEBHOOK_SECRET.", file=sys.stderr)
        return 1
    payload = {
        "url": _webhook_url(public),
        "update_types": ["message_created", "bot_started"],
        "secret": secret,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    post_headers = {**headers, "Content-Type": "application/json; charset=utf-8"}
    try:
        status, raw = max_request("POST", _subscriptions_url(api), content=body, headers=post_headers)
    except httpx.HTTPError as exc:
        print(f"Max сеть: {exc}", file=sys.stderr)
        return 1
    print("Подписка:")
    try:
        _print_body(status, raw)
    except SystemExit:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
