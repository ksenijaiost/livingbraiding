#!/usr/bin/env bash
# Запуск API на проде: сначала alembic upgrade head, потом uvicorn.
#
# Локально/VPS: из каталога backend/ с активированным venv:
#   ./scripts/start_uvicorn.sh
#
# DigitalOcean App Platform: скрипт сам по себе не подставится — в UI укажите
# «Run command» явно (см. backend/README.md → App Platform). Достаточно:
#   bash scripts/start_uvicorn.sh
# если рабочая директория сервиса уже backend/; иначе полный путь от корня репо.
#
# Порт/хост: HOST=127.0.0.1 PORT=8010 ./scripts/start_uvicorn.sh

set -euo pipefail
cd "$(dirname "$0")/.."

alembic upgrade head

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-80}"
exec python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
