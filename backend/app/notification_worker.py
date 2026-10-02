"""Фоновый воркер notification_outbox (вместо cron на Timeweb Apps)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.db.session import SessionLocal
from app.notifications import DEFAULT_MAX_ATTEMPTS, DEFAULT_OUTBOX_LIMIT, process_outbox
from app.settings import get_settings

logger = logging.getLogger(__name__)

# Ссылка на активную задачу — для тестов / диагностики.
_worker_task: asyncio.Task[Any] | None = None


def is_notification_worker_running() -> bool:
    return _worker_task is not None and not _worker_task.done()


def run_outbox_tick(
    *,
    limit: int = DEFAULT_OUTBOX_LIMIT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, int]:
    """Один проход process_outbox в своей сессии БД (синхронно)."""
    with SessionLocal() as db:
        stats = process_outbox(db, limit=limit, max_attempts=max_attempts)
        # process_outbox сам коммитит по строкам; финальный commit на всякий случай.
        try:
            db.commit()
        except Exception:
            db.rollback()
        return stats


async def notification_worker_loop(
    stop: asyncio.Event,
    *,
    interval_seconds: float | None = None,
) -> None:
    """Цикл: process_outbox в thread pool, пауза, повтор до stop."""
    settings = get_settings()
    interval = float(
        interval_seconds
        if interval_seconds is not None
        else settings.notification_worker_interval_seconds
    )
    logger.info(
        "notification worker started (interval=%ss)",
        interval,
    )
    while not stop.is_set():
        try:
            stats = await asyncio.to_thread(run_outbox_tick)
            if stats.get("processed"):
                logger.info(
                    "notification worker tick: processed=%s sent=%s failed=%s",
                    stats.get("processed"),
                    stats.get("sent"),
                    stats.get("failed"),
                )
        except Exception:
            logger.exception("notification worker tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    logger.info("notification worker stopped")


def start_notification_worker() -> tuple[asyncio.Task[Any], asyncio.Event] | None:
    """Запустить фоновую задачу, если включено в настройках. Иначе None."""
    global _worker_task
    settings = get_settings()
    if not settings.notification_worker_enabled:
        logger.info("notification worker disabled (NOTIFICATION_WORKER_ENABLED=false)")
        return None
    stop = asyncio.Event()
    task = asyncio.create_task(
        notification_worker_loop(stop),
        name="notification-outbox-worker",
    )
    _worker_task = task
    return task, stop


async def stop_notification_worker(
    handle: tuple[asyncio.Task[Any], asyncio.Event] | None,
) -> None:
    global _worker_task
    if handle is None:
        return
    task, stop = handle
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=15)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    _worker_task = None
