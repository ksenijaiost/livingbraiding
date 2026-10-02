#!/usr/bin/env bash
# Запасной ручной прогон notification_outbox (фоновый воркер в приложении — основной путь).
# Запускать из backend/ или с ROOT, указывая путь. См. backend/README.md.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${BACKEND_DIR}"
exec python -m app.process_notification_outbox "$@"
