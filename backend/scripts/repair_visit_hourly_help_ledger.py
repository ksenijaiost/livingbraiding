"""Выровнять нетто почасовой помощи в журнале под карточку визита.

После бага двойного сторно в журнале остаются лишние сторно; пересохранение визита
их не убирает. Скрипт пишет корректирующее начисление.

Запуск из каталога backend:

  python scripts/repair_visit_hourly_help_ledger.py --dry-run 270 303
  python scripts/repair_visit_hourly_help_ledger.py 270 303
  python scripts/repair_visit_hourly_help_ledger.py --by-user 1 270 303
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Позволяет запускать файл напрямую: python scripts/...
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from sqlalchemy import select

from app.db.models import User, Visit
from app.db.session import SessionLocal
from app.payroll_fund import (
    HOURLY_HELP_CORRECTION_COMMENT,
    repair_visit_hourly_help_ledger_net,
    visit_hourly_help_ledger_net_by_user,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("visit_ids", nargs="+", type=int, help="ID визитов")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Только показать дельты, не писать в журнал",
    )
    p.add_argument(
        "--by-user",
        type=int,
        default=None,
        help="ID пользователя, от имени которого пишется коррекция (по умолчанию — первый активный)",
    )
    args = p.parse_args(argv)

    with SessionLocal() as db:
        actor_id = args.by_user
        if actor_id is None:
            actor = db.scalar(
                select(User)
                .where(User.is_active.is_(True))
                .order_by(User.id.asc())
                .limit(1)
            )
            if actor is None:
                print("Нет пользователей в БД.", file=sys.stderr)
                return 1
            actor_id = int(actor.id)
            print(f"created_by_user_id={actor_id} ({actor.display_name})")

        any_posted = False
        for vid in args.visit_ids:
            visit = db.get(Visit, int(vid))
            if visit is None:
                print(f"визит {vid}: не найден", file=sys.stderr)
                continue
            before = visit_hourly_help_ledger_net_by_user(db, int(vid))
            rows = repair_visit_hourly_help_ledger_net(
                db, visit, actor_id, dry_run=args.dry_run
            )
            if not rows:
                print(f"визит {vid}: уже ок, нетто={before}")
                continue
            for r in rows:
                print(
                    f"визит {vid}: user#{r['user_id']} "
                    f"actual={r['actual']} expected={r['expected']} delta={r['delta']}"
                    + (" (dry-run)" if args.dry_run else f" → {HOURLY_HELP_CORRECTION_COMMENT}")
                )
            any_posted = True
        if not args.dry_run and any_posted:
            db.commit()
            print("commit ok")
        elif args.dry_run:
            print("dry-run: commit не выполнялся")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
