"""CLI: повторная отправка notification_outbox (pending / failed с лимитом попыток).

Запуск из каталога backend:

  python -m app.process_notification_outbox
  python -m app.process_notification_outbox --limit 100 --max-attempts 5
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.db.session import SessionLocal
from app.notifications import DEFAULT_MAX_ATTEMPTS, DEFAULT_OUTBOX_LIMIT, process_outbox

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("process_notification_outbox")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_OUTBOX_LIMIT,
        help=f"Максимум записей за запуск (по умолчанию {DEFAULT_OUTBOX_LIMIT})",
    )
    p.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help=f"Лимит попыток для failed (по умолчанию {DEFAULT_MAX_ATTEMPTS})",
    )
    args = p.parse_args(argv)
    with SessionLocal() as db:
        stats = process_outbox(db, limit=int(args.limit), max_attempts=int(args.max_attempts))
        db.commit()
    logger.info(
        "outbox done: processed=%s sent=%s failed=%s",
        stats["processed"],
        stats["sent"],
        stats["failed"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
