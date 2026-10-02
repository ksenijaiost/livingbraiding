#!/usr/bin/env bash
# Повторная отправка notification_outbox (pending / failed с лимитом попыток).
# Запускать из backend/ или с ROOT, указывая путь. Пример cron — в backend/README.md.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${BACKEND_DIR}"
exec python -m app.process_notification_outbox "$@"
